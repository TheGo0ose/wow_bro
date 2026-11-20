"""Interactive HTML coverage map built with folium (Leaflet)."""

from __future__ import annotations

import html
from pathlib import Path

import folium

from coverage.grid import Cell, Level
from coverage.placement import COVERAGE_RADIUS_M
from coverage.pipeline import Analysis

LEVEL_COLORS = {
    Level.GOOD: "#2e9d4f",
    Level.FAIR: "#f2c230",
    Level.WEAK: "#d7301f",
}
LEVEL_LABELS = {
    Level.GOOD: "≥ −90 дБм — хорошее",
    Level.FAIR: "−105…−90 дБм — среднее",
    Level.WEAK: "< −105 дБм — слабая зона",
}
SPARSE_COLOR = "#9e9e9e"
CHOSEN_COLOR = "#6a1b9a"


def _cell_tooltip(cell: Cell) -> str:
    return f"{cell.cell_id}: медиана {cell.median_dbm:g} дБм, замеров {cell.samples}"


def _add_cells(fmap: folium.Map, analysis: Analysis) -> None:
    grid = analysis.bounds.grid
    layer = folium.FeatureGroup(name="Квадраты 200×200 м (медиана RSRP)")
    for cell in analysis.grid.cells:
        color = LEVEL_COLORS[cell.level]
        folium.Rectangle(
            bounds=list(grid.cell_corners(cell.ix, cell.iy)),
            color=color,
            weight=0.5,
            fill=True,
            fill_color=color,
            fill_opacity=0.55,
            tooltip=_cell_tooltip(cell),
        ).add_to(layer)
    layer.add_to(fmap)

    sparse = folium.FeatureGroup(name="Мало замеров (< 5), не учитываются", show=False)
    for cell in analysis.grid.sparse_cells:
        folium.Rectangle(
            bounds=list(grid.cell_corners(cell.ix, cell.iy)),
            color=SPARSE_COLOR,
            weight=1,
            dash_array="4",
            fill=True,
            fill_opacity=0.2,
            tooltip=_cell_tooltip(cell),
        ).add_to(sparse)
    sparse.add_to(fmap)

    worst = folium.FeatureGroup(name="Топ-3 худших квадрата")
    for rank, cell in enumerate(analysis.worst, start=1):
        folium.Rectangle(
            bounds=list(grid.cell_corners(cell.ix, cell.iy)),
            color="#000000",
            weight=3,
            fill=False,
            tooltip=f"#{rank} худший — {_cell_tooltip(cell)}",
        ).add_to(worst)
        folium.Marker(
            location=(cell.center_lat, cell.center_lon),
            icon=folium.DivIcon(
                html=f'<div style="font:bold 13px sans-serif;color:#fff;'
                f'text-shadow:0 0 3px #000;transform:translate(-4px,-8px)">{rank}</div>'
            ),
        ).add_to(worst)
    worst.add_to(fmap)


def _add_sites(fmap: folium.Map, analysis: Analysis) -> None:
    towers = folium.FeatureGroup(name="Существующие БС")
    for tower in analysis.towers:
        folium.Marker(
            location=(tower.lat, tower.lon),
            tooltip=f"БС {tower.id}",
            icon=folium.Icon(color="blue", icon="wifi", prefix="fa"),
        ).add_to(towers)
    towers.add_to(fmap)

    chosen_ids = set(analysis.placement.site_ids)
    candidates = folium.FeatureGroup(name="Площадки-кандидаты")
    for cand in analysis.candidates:
        if cand.id in chosen_ids:
            continue
        if cand.id in analysis.eligibility.rejected:
            distance = analysis.eligibility.rejected[cand.id]
            tooltip = f"{cand.id}: недопустима, до БС {distance:.0f} м (< 500 м)"
            color, fill = "#757575", "#ffffff"
        else:
            covered = len(analysis.coverage.get(cand.id, ()))
            tooltip = f"{cand.id}: покрывает {covered} красных квадратов"
            color, fill = "#37474f", "#90a4ae"
        folium.CircleMarker(
            location=(cand.lat, cand.lon),
            radius=6,
            color=color,
            weight=2,
            fill=True,
            fill_color=fill,
            fill_opacity=0.9,
            tooltip=tooltip,
        ).add_to(candidates)
    candidates.add_to(fmap)

    chosen = folium.FeatureGroup(name="Выбранные площадки и радиус 1 км")
    by_id = {cand.id: cand for cand in analysis.candidates}
    for site_id in analysis.placement.site_ids:
        cand = by_id[site_id]
        covered = len(analysis.coverage.get(site_id, ()))
        folium.Circle(
            location=(cand.lat, cand.lon),
            radius=COVERAGE_RADIUS_M,
            color=CHOSEN_COLOR,
            weight=2,
            fill=True,
            fill_opacity=0.08,
        ).add_to(chosen)
        folium.Marker(
            location=(cand.lat, cand.lon),
            tooltip=f"Новая БС {site_id}: покрывает {covered} красных квадратов",
            icon=folium.Icon(color="purple", icon="star", prefix="fa"),
        ).add_to(chosen)
    chosen.add_to(fmap)


def _legend_html(analysis: Analysis) -> str:
    placement = analysis.placement
    weak_total = len(analysis.grid.weak_cells)
    rows = "".join(
        f'<div><span style="display:inline-block;width:14px;height:14px;'
        f'background:{LEVEL_COLORS[level]};margin-right:6px;vertical-align:middle"></span>'
        f"{html.escape(LEVEL_LABELS[level])}</div>"
        for level in Level
    )
    pair = " + ".join(placement.site_ids) or "—"
    return f"""
    <div style="position:fixed;bottom:24px;left:12px;z-index:9999;background:#fff;
                padding:10px 12px;border-radius:6px;box-shadow:0 1px 6px rgba(0,0,0,.3);
                font:13px/1.5 sans-serif;color:#222;max-width:280px">
      <div style="font-weight:bold;margin-bottom:4px">Покрытие района (RSRP, медиана)</div>
      {rows}
      <hr style="margin:6px 0;border:none;border-top:1px solid #ddd">
      <div>Отброшено строк: <b>{analysis.cleaning.dropped_rows}</b></div>
      <div>Новые БС: <b>{html.escape(pair)}</b></div>
      <div>Покрыто слабых квадратов: <b>{placement.covered_count}</b> из {weak_total}</div>
    </div>
    """


def build_map(analysis: Analysis) -> folium.Map:
    b = analysis.bounds
    # Esri is the default: it needs no API key and works when the HTML is opened straight
    # from disk. OpenStreetMap blocks such requests (file://, no Referer), so it is optional.
    fmap = folium.Map(location=b.center, zoom_start=14, control_scale=True, tiles=None)
    folium.TileLayer(
        tiles="https://server.arcgisonline.com/ArcGIS/rest/services/World_Street_Map/MapServer/tile/{z}/{y}/{x}",
        attr="Tiles © Esri",
        name="Esri Street Map",
        max_zoom=19,
    ).add_to(fmap)
    folium.TileLayer("OpenStreetMap", name="OpenStreetMap (только по http)", show=False).add_to(fmap)
    folium.Rectangle(
        bounds=[(b.lat_min, b.lon_min), (b.lat_max, b.lon_max)],
        color="#1565c0",
        weight=2,
        fill=False,
        tooltip="Граница района",
    ).add_to(fmap)

    _add_cells(fmap, analysis)
    _add_sites(fmap, analysis)
    fmap.get_root().html.add_child(folium.Element(_legend_html(analysis)))
    folium.LayerControl(collapsed=False).add_to(fmap)
    fmap.fit_bounds([(b.lat_min, b.lon_min), (b.lat_max, b.lon_max)])
    return fmap


def render_map(analysis: Analysis, path: Path) -> None:
    build_map(analysis).save(str(path))
