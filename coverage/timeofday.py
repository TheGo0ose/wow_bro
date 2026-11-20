"""Bonus: day vs night coverage.

Measurements are split by local time (bounds.json timezone, Asia/Bishkek):
day is [07:00, 22:00), night is [22:00, 07:00). Each half gets its own grid with the
same rules (median, at least 5 samples per cell). Cells that have enough samples in
both halves are compared directly.

Why not an hourly slider: with ~12 samples per cell in total, a 1-hour window leaves
no cell with 5 samples, so day/night is the finest split the data supports.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import tzinfo
from typing import Iterable

from coverage.cleaning import Measurement
from coverage.geo import LocalGrid
from coverage.grid import Cell, GridResult, Level, build_grid

DAY_START_HOUR = 7
NIGHT_START_HOUR = 22

_LEVEL_RANK = {Level.WEAK: 0, Level.FAIR: 1, Level.GOOD: 2}


def is_daytime(measurement: Measurement, tz: tzinfo) -> bool:
    hour = measurement.timestamp.astimezone(tz).hour
    return DAY_START_HOUR <= hour < NIGHT_START_HOUR


@dataclass(frozen=True)
class CellChange:
    day: Cell
    night: Cell

    @property
    def cell_id(self) -> str:
        return self.day.cell_id

    @property
    def delta_db(self) -> float:
        """Night median minus day median: positive means the signal is better at night."""
        return round(self.night.median_dbm - self.day.median_dbm, 6)

    @property
    def level_shift(self) -> int:
        """+1 / +2 if the colour improves at night, negative if it gets worse, 0 if the same."""
        return _LEVEL_RANK[self.night.level] - _LEVEL_RANK[self.day.level]


@dataclass(frozen=True)
class DayNightComparison:
    day_samples: int
    night_samples: int
    day: GridResult
    night: GridResult
    common: list[CellChange]  # cells with >= 5 samples both by day and by night

    @property
    def median_delta_db(self) -> float | None:
        if not self.common:
            return None
        return round(statistics.median(c.delta_db for c in self.common), 6)

    @property
    def better_at_night(self) -> list[CellChange]:
        return [c for c in self.common if c.level_shift > 0]

    @property
    def worse_at_night(self) -> list[CellChange]:
        return [c for c in self.common if c.level_shift < 0]


def compare_day_night(
    measurements: Iterable[Measurement], grid: LocalGrid, tz: tzinfo
) -> DayNightComparison:
    day: list[Measurement] = []
    night: list[Measurement] = []
    for m in measurements:
        (day if is_daytime(m, tz) else night).append(m)

    day_grid = build_grid(day, grid)
    night_grid = build_grid(night, grid)
    night_by_id = {cell.cell_id: cell for cell in night_grid.cells}
    common = [
        CellChange(day=cell, night=night_by_id[cell.cell_id])
        for cell in day_grid.cells
        if cell.cell_id in night_by_id
    ]
    return DayNightComparison(
        day_samples=len(day),
        night_samples=len(night),
        day=day_grid,
        night=night_grid,
        common=common,
    )
