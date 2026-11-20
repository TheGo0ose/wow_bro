from coverage.geo import LocalGrid
from coverage.grid import Cell
from coverage.placement import best_pair, check_eligibility, coverage_sets, greedy_sites
from coverage.sites import Site

GRID = LocalGrid(origin_lat=42.84, origin_lon=74.56, cell_size_m=200)


def _site(site_id, x, y):
    lat, lon = GRID.to_latlon(x, y)
    return Site(site_id, lat, lon)


def _weak_cell(ix, iy):
    lat, lon = GRID.cell_center(ix, iy)
    return Cell(ix=ix, iy=iy, samples=5, median_dbm=-120, center_lat=lat, center_lon=lon)


def test_candidate_closer_than_500m_to_any_tower_is_rejected():
    towers = [_site("B1", 0, 0), _site("B2", 5000, 0)]
    candidates = [_site("C1", 400, 0), _site("C2", 2000, 0), _site("C3", 4600, 0)]
    result = check_eligibility(candidates, towers)
    assert [c.id for c in result.allowed] == ["C2"]
    assert set(result.rejected) == {"C1", "C3"}


def test_exactly_500m_is_allowed():
    tower = Site("B1", 42.85, 74.58)
    # move north by exactly 500 m: dlat = 500 / R in degrees
    cand = Site("C1", 42.85 + 500 / 6_371_000 * 180 / 3.141592653589793, 74.58)
    assert [c.id for c in check_eligibility([cand], [tower]).allowed] == ["C1"]


def test_no_towers_means_every_candidate_is_allowed():
    assert len(check_eligibility([_site("C1", 0, 0)], []).allowed) == 1


def test_coverage_radius_is_inclusive_1km():
    cand = _site("C1", 100, 100)  # centre of cell (0, 0)
    near = _weak_cell(4, 0)  # centre 800 m east
    far = _weak_cell(6, 0)  # centre 1200 m east
    assert coverage_sets([cand], [near, far]) == {"C1": frozenset({near.cell_id})}


def test_best_pair_maximises_union_not_sum():
    coverage = {
        "C1": frozenset({"a", "b", "c", "d"}),
        "C2": frozenset({"a", "b", "c", "e"}),  # big but overlaps C1
        "C3": frozenset({"x", "y", "z"}),
    }
    result = best_pair(coverage)
    assert result.site_ids == ("C1", "C3")
    assert result.covered_count == 7
    assert result.pairs_checked == 3


def test_best_pair_tie_goes_to_smallest_ids():
    coverage = {
        "C09": frozenset({"a"}),
        "C02": frozenset({"b"}),
        "C05": frozenset({"a"}),
        "C01": frozenset({"a"}),
    }
    # pairs covering 2 cells: (C01,C02), (C02,C05), (C02,C09) -> smallest is (C01,C02)
    assert best_pair(coverage).site_ids == ("C01", "C02")


def test_best_pair_with_fewer_than_two_candidates():
    assert best_pair({"C1": frozenset({"a"})}).site_ids == ("C1",)
    assert best_pair({}).site_ids == ()


def test_greedy_can_miss_the_optimum():
    # C1 is the best single site but overlaps both others; C2 + C3 together are better.
    coverage = {
        "C1": frozenset({"a", "b", "c", "d"}),
        "C2": frozenset({"a", "b", "x"}),
        "C3": frozenset({"c", "d", "y"}),
    }
    greedy = greedy_sites(coverage)
    assert [s.site_id for s in greedy.steps] == ["C1", "C2"]
    assert [s.new_cells for s in greedy.steps] == [4, 1]
    assert greedy.covered_count == 5
    assert best_pair(coverage).covered_count == 6


def test_greedy_tie_goes_to_smallest_id():
    coverage = {"C3": frozenset({"a"}), "C1": frozenset({"b"}), "C2": frozenset({"c"})}
    assert greedy_sites(coverage).site_ids == ("C1", "C2")


def test_greedy_with_too_few_candidates():
    assert greedy_sites({"C1": frozenset({"a"})}).site_ids == ("C1",)
    assert greedy_sites({}).covered_count == 0
