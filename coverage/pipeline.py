"""End-to-end analysis: load -> clean -> grid -> choose sites."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coverage.cleaning import CleaningResult, load_measurements
from coverage.config import Bounds, load_bounds
from coverage.grid import Cell, GridResult, build_grid, worst_cells
from coverage.placement import Eligibility, Placement, best_pair, check_eligibility, coverage_sets
from coverage.sites import Site, load_sites

WORST_CELLS_COUNT = 3


@dataclass(frozen=True)
class InputPaths:
    measurements: Path
    towers: Path
    candidates: Path
    bounds: Path

    @classmethod
    def from_dir(cls, data_dir: Path) -> "InputPaths":
        data_dir = Path(data_dir)
        return cls(
            measurements=data_dir / "measurements.csv",
            towers=data_dir / "towers.csv",
            candidates=data_dir / "candidates.csv",
            bounds=data_dir / "bounds.json",
        )


@dataclass(frozen=True)
class Analysis:
    bounds: Bounds
    cleaning: CleaningResult
    grid: GridResult
    worst: list[Cell]
    towers: list[Site]
    candidates: list[Site]
    eligibility: Eligibility
    coverage: dict[str, frozenset[str]]  # allowed candidate id -> covered weak cell ids
    placement: Placement


def run_analysis(paths: InputPaths) -> Analysis:
    bounds = load_bounds(paths.bounds)
    cleaning = load_measurements(paths.measurements, bounds)
    grid = build_grid(cleaning.measurements, bounds.grid)

    towers = load_sites(paths.towers)
    candidates = load_sites(paths.candidates)
    eligibility = check_eligibility(candidates, towers, radius_m=bounds.earth_radius_m)
    coverage = coverage_sets(eligibility.allowed, grid.weak_cells, radius_m=bounds.earth_radius_m)

    return Analysis(
        bounds=bounds,
        cleaning=cleaning,
        grid=grid,
        worst=worst_cells(grid.cells, WORST_CELLS_COUNT),
        towers=towers,
        candidates=candidates,
        eligibility=eligibility,
        coverage=coverage,
        placement=best_pair(coverage),
    )
