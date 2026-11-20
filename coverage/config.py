"""District bounds and grid parameters loaded from bounds.json."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from coverage.errors import DataError
from coverage.geo import EARTH_RADIUS_M, LocalGrid

DEFAULT_TIMEZONE = "Asia/Bishkek"


@dataclass(frozen=True)
class Bounds:
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    grid_origin_lat: float
    grid_origin_lon: float
    cell_size_m: float = 200.0
    earth_radius_m: float = EARTH_RADIUS_M
    timezone: str = DEFAULT_TIMEZONE

    def contains(self, lat: float, lon: float) -> bool:
        """Rectangle check with inclusive borders."""
        return self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max

    @property
    def grid(self) -> LocalGrid:
        return LocalGrid(
            origin_lat=self.grid_origin_lat,
            origin_lon=self.grid_origin_lon,
            cell_size_m=self.cell_size_m,
            radius_m=self.earth_radius_m,
        )

    @property
    def center(self) -> tuple[float, float]:
        return (self.lat_min + self.lat_max) / 2, (self.lon_min + self.lon_max) / 2


def load_bounds(path: Path) -> Bounds:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise DataError(f"bounds file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DataError(f"bounds file is not valid JSON: {path}: {exc}") from exc

    try:
        return Bounds(
            lat_min=float(raw["lat_min"]),
            lat_max=float(raw["lat_max"]),
            lon_min=float(raw["lon_min"]),
            lon_max=float(raw["lon_max"]),
            # The grid origin defaults to the south-west corner of the district.
            grid_origin_lat=float(raw.get("grid_origin_lat", raw["lat_min"])),
            grid_origin_lon=float(raw.get("grid_origin_lon", raw["lon_min"])),
            cell_size_m=float(raw.get("cell_size_m", 200.0)),
            earth_radius_m=float(raw.get("earth_radius_m", EARTH_RADIUS_M)),
            timezone=str(raw.get("timezone", DEFAULT_TIMEZONE)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DataError(f"bounds file has missing or invalid fields: {path}: {exc}") from exc
