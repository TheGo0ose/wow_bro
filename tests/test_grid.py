from datetime import datetime, timedelta, timezone

import pytest

from coverage.cleaning import Measurement
from coverage.geo import LocalGrid
from coverage.grid import Cell, Level, build_grid, classify, make_cell_id, worst_cells

GRID = LocalGrid(origin_lat=42.84, origin_lon=74.56, cell_size_m=200)
T0 = datetime(2026, 11, 16, 12, tzinfo=timezone(timedelta(hours=6)))


def _points_in_cell(ix, iy, values):
    lat, lon = GRID.cell_center(ix, iy)
    return [Measurement(lat, lon, v, T0 + timedelta(seconds=i)) for i, v in enumerate(values)]


@pytest.mark.parametrize("median, level", [
    (-80, Level.GOOD), (-90, Level.GOOD), (-90.01, Level.FAIR),
    (-105, Level.FAIR), (-105.01, Level.WEAK), (-130, Level.WEAK),
])
def test_classify_thresholds(median, level):
    assert classify(median) is level


def test_cell_id_format():
    assert make_cell_id(6, 13) == "X006_Y013"


def test_median_and_min_samples():
    data = _points_in_cell(1, 2, [-100, -110, -90, -120, -95, -101]) + _points_in_cell(3, 3, [-80] * 4)
    result = build_grid(data, GRID)
    assert [c.cell_id for c in result.cells] == ["X001_Y002"]
    assert result.cells[0].median_dbm == -100.5
    assert result.cells[0].samples == 6
    assert [c.cell_id for c in result.sparse_cells] == ["X003_Y003"]


def test_exactly_five_samples_is_enough():
    result = build_grid(_points_in_cell(0, 0, [-100] * 5), GRID)
    assert len(result.cells) == 1


def test_median_has_no_float_noise():
    result = build_grid(_points_in_cell(0, 0, [-120.7, -120.8, -120.7, -120.8, -121, -120]), GRID)
    assert result.cells[0].median_dbm == -120.75


def _cell(ix, median):
    return Cell(ix=ix, iy=0, samples=5, median_dbm=median, center_lat=0, center_lon=0)


def test_worst_cells_sorted_by_median_then_id():
    cells = [_cell(5, -110), _cell(2, -120), _cell(1, -110), _cell(9, -90)]
    assert [c.cell_id for c in worst_cells(cells)] == ["X002_Y000", "X001_Y000", "X005_Y000"]
