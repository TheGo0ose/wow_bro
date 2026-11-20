"""200x200 m grid: per-cell RSRP median, colour levels and worst cells."""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Iterable

from coverage.cleaning import Measurement
from coverage.geo import LocalGrid

MIN_SAMPLES_PER_CELL = 5
GOOD_THRESHOLD_DBM = -90.0  # median >= -90 -> green
WEAK_THRESHOLD_DBM = -105.0  # median < -105 -> red; [-105, -90) -> yellow
# Medians are rounded to drop float noise such as -120.75000000000001.
MEDIAN_DECIMALS = 6


class Level(str, Enum):
    GOOD = "green"
    FAIR = "yellow"
    WEAK = "red"


def classify(median_dbm: float) -> Level:
    if median_dbm >= GOOD_THRESHOLD_DBM:
        return Level.GOOD
    if median_dbm >= WEAK_THRESHOLD_DBM:
        return Level.FAIR
    return Level.WEAK


def make_cell_id(ix: int, iy: int) -> str:
    return f"X{ix:03d}_Y{iy:03d}"


@dataclass(frozen=True)
class Cell:
    ix: int
    iy: int
    samples: int
    median_dbm: float
    center_lat: float
    center_lon: float

    @property
    def cell_id(self) -> str:
        return make_cell_id(self.ix, self.iy)

    @property
    def level(self) -> Level:
        return classify(self.median_dbm)


@dataclass
class GridResult:
    cells: list[Cell]  # cells with enough samples, sorted by cell_id
    sparse_cells: list[Cell]  # cells with fewer than min_samples, ignored by the analysis

    @property
    def weak_cells(self) -> list[Cell]:
        return [cell for cell in self.cells if cell.level is Level.WEAK]

    def level_counts(self) -> dict[Level, int]:
        counts = {level: 0 for level in Level}
        for cell in self.cells:
            counts[cell.level] += 1
        return counts


def build_grid(
    measurements: Iterable[Measurement], grid: LocalGrid, min_samples: int = MIN_SAMPLES_PER_CELL
) -> GridResult:
    samples: dict[tuple[int, int], list[float]] = defaultdict(list)
    for m in measurements:
        samples[grid.cell_of(m.lat, m.lon)].append(m.rsrp_dbm)

    cells: list[Cell] = []
    sparse: list[Cell] = []
    for (ix, iy), values in samples.items():
        center_lat, center_lon = grid.cell_center(ix, iy)
        cell = Cell(
            ix=ix,
            iy=iy,
            samples=len(values),
            median_dbm=round(statistics.median(values), MEDIAN_DECIMALS),
            center_lat=center_lat,
            center_lon=center_lon,
        )
        (cells if len(values) >= min_samples else sparse).append(cell)

    by_id = lambda c: c.cell_id  # noqa: E731
    return GridResult(cells=sorted(cells, key=by_id), sparse_cells=sorted(sparse, key=by_id))


def worst_cells(cells: Iterable[Cell], count: int = 3) -> list[Cell]:
    """Cells with the lowest median; ties are broken by cell_id."""
    return sorted(cells, key=lambda c: (c.median_dbm, c.cell_id))[:count]
