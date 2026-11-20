"""Geometry helpers: haversine distance and the task's local equirectangular grid."""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_000.0


def haversine_m(
    lat1: float, lon1: float, lat2: float, lon2: float, radius_m: float = EARTH_RADIUS_M
) -> float:
    """Great-circle distance between two points in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = phi2 - phi1
    d_lambda = math.radians(lon2 - lon1)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius_m * math.asin(math.sqrt(min(1.0, h)))


@dataclass(frozen=True)
class LocalGrid:
    """Square grid in a local equirectangular projection (see CLARIFICATIONS, section 7).

    x = R * cos(lat0) * rad(lon - lon0), y = R * rad(lat - lat0),
    cell = (floor(x / size), floor(y / size)).
    """

    origin_lat: float
    origin_lon: float
    cell_size_m: float
    radius_m: float = EARTH_RADIUS_M

    @property
    def _cos_lat0(self) -> float:
        return math.cos(math.radians(self.origin_lat))

    def to_xy(self, lat: float, lon: float) -> tuple[float, float]:
        x = self.radius_m * self._cos_lat0 * math.radians(lon - self.origin_lon)
        y = self.radius_m * math.radians(lat - self.origin_lat)
        return x, y

    def to_latlon(self, x: float, y: float) -> tuple[float, float]:
        lat = self.origin_lat + math.degrees(y / self.radius_m)
        lon = self.origin_lon + math.degrees(x / (self.radius_m * self._cos_lat0))
        return lat, lon

    def cell_of(self, lat: float, lon: float) -> tuple[int, int]:
        x, y = self.to_xy(lat, lon)
        return math.floor(x / self.cell_size_m), math.floor(y / self.cell_size_m)

    def cell_center(self, ix: int, iy: int) -> tuple[float, float]:
        return self.to_latlon((ix + 0.5) * self.cell_size_m, (iy + 0.5) * self.cell_size_m)

    def cell_corners(self, ix: int, iy: int) -> tuple[tuple[float, float], tuple[float, float]]:
        """South-west and north-east corners as (lat, lon) pairs."""
        south_west = self.to_latlon(ix * self.cell_size_m, iy * self.cell_size_m)
        north_east = self.to_latlon((ix + 1) * self.cell_size_m, (iy + 1) * self.cell_size_m)
        return south_west, north_east
