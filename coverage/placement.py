"""Choice of new base station sites.

A candidate is allowed only if it is at least 500 m from EVERY existing tower.
A candidate covers a weak (red) cell if the haversine distance to the cell centre
is at most 1000 m. The best pair maximises the size of the UNION of covered cells;
ties go to the lexicographically smallest sorted pair of IDs.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from typing import Iterable, Mapping

from coverage.geo import EARTH_RADIUS_M, haversine_m
from coverage.grid import Cell
from coverage.sites import Site

log = logging.getLogger(__name__)

MIN_DISTANCE_TO_TOWER_M = 500.0
COVERAGE_RADIUS_M = 1000.0


@dataclass(frozen=True)
class Eligibility:
    allowed: list[Site]
    rejected: dict[str, float]  # candidate id -> distance to the nearest tower, m


@dataclass(frozen=True)
class Placement:
    site_ids: tuple[str, ...]
    covered_cells: frozenset[str]
    pairs_checked: int

    @property
    def covered_count(self) -> int:
        return len(self.covered_cells)


def check_eligibility(
    candidates: Iterable[Site],
    towers: Iterable[Site],
    min_distance_m: float = MIN_DISTANCE_TO_TOWER_M,
    radius_m: float = EARTH_RADIUS_M,
) -> Eligibility:
    towers = list(towers)
    allowed: list[Site] = []
    rejected: dict[str, float] = {}
    for cand in candidates:
        nearest = min(
            (haversine_m(cand.lat, cand.lon, t.lat, t.lon, radius_m) for t in towers),
            default=float("inf"),
        )
        if nearest >= min_distance_m:
            allowed.append(cand)
        else:
            rejected[cand.id] = nearest
    return Eligibility(allowed=allowed, rejected=rejected)


def coverage_sets(
    candidates: Iterable[Site],
    weak_cells: Iterable[Cell],
    radius_coverage_m: float = COVERAGE_RADIUS_M,
    radius_m: float = EARTH_RADIUS_M,
) -> dict[str, frozenset[str]]:
    """For every candidate, the set of weak cell IDs within the coverage radius."""
    weak_cells = list(weak_cells)
    return {
        cand.id: frozenset(
            cell.cell_id
            for cell in weak_cells
            if haversine_m(cand.lat, cand.lon, cell.center_lat, cell.center_lon, radius_m)
            <= radius_coverage_m
        )
        for cand in candidates
    }


def best_pair(coverage: Mapping[str, frozenset[str]]) -> Placement:
    """Exhaustive search over all pairs.

    itertools.combinations over sorted IDs yields pairs in lexicographic order,
    so keeping only strictly better pairs implements the tie-break rule.
    """
    ids = sorted(coverage)
    if len(ids) < 2:
        log.warning("only %d allowed candidate(s), a pair cannot be formed", len(ids))
        covered = frozenset().union(*(coverage[i] for i in ids))
        return Placement(site_ids=tuple(ids), covered_cells=covered, pairs_checked=0)

    best: Placement | None = None
    checked = 0
    for a, b in itertools.combinations(ids, 2):
        checked += 1
        union = coverage[a] | coverage[b]
        if best is None or len(union) > best.covered_count:
            best = Placement(site_ids=(a, b), covered_cells=union, pairs_checked=0)

    assert best is not None
    return Placement(site_ids=best.site_ids, covered_cells=best.covered_cells, pairs_checked=checked)


@dataclass(frozen=True)
class GreedyStep:
    site_id: str
    new_cells: int  # weak cells this pick added on top of the previous picks


@dataclass(frozen=True)
class GreedyPlacement:
    steps: tuple[GreedyStep, ...]
    covered_cells: frozenset[str]

    @property
    def site_ids(self) -> tuple[str, ...]:
        return tuple(sorted(step.site_id for step in self.steps))

    @property
    def covered_count(self) -> int:
        return len(self.covered_cells)


def greedy_sites(coverage: Mapping[str, frozenset[str]], count: int = 2) -> GreedyPlacement:
    """Bonus baseline: repeatedly take the site that adds the most uncovered cells.

    Fast (count * n checks instead of all combinations) but not guaranteed optimal:
    a strong first pick may overlap with every good second pick.
    Ties go to the smallest ID, as in the exhaustive search.
    """
    remaining = sorted(coverage)
    covered: frozenset[str] = frozenset()
    steps: list[GreedyStep] = []
    for _ in range(min(count, len(remaining))):
        pick = max(remaining, key=lambda sid: (len(coverage[sid] - covered), -remaining.index(sid)))
        steps.append(GreedyStep(site_id=pick, new_cells=len(coverage[pick] - covered)))
        covered |= coverage[pick]
        remaining.remove(pick)
    return GreedyPlacement(steps=tuple(steps), covered_cells=covered)
