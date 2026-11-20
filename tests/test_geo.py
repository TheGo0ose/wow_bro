import pytest

from coverage.geo import LocalGrid, haversine_m


def test_haversine_zero_distance():
    assert haversine_m(42.85, 74.58, 42.85, 74.58) == 0


def test_haversine_one_degree_of_latitude():
    # 1 degree along a meridian = R * pi / 180 = 111 194.9 m for R = 6 371 000
    assert haversine_m(42.0, 74.0, 43.0, 74.0) == pytest.approx(111_194.93, abs=0.1)


def test_haversine_is_symmetric():
    a = haversine_m(42.84, 74.56, 42.87, 74.60)
    b = haversine_m(42.87, 74.60, 42.84, 74.56)
    assert a == pytest.approx(b)


@pytest.fixture
def grid():
    return LocalGrid(origin_lat=42.84, origin_lon=74.56, cell_size_m=200)


def test_origin_is_in_cell_zero(grid):
    assert grid.cell_of(42.84, 74.56) == (0, 0)


def test_cell_index_uses_floor(grid):
    lat, lon = grid.to_latlon(399.9, 200.1)
    assert grid.cell_of(lat, lon) == (1, 1)
    lat, lon = grid.to_latlon(-0.1, 0)
    assert grid.cell_of(lat, lon)[0] == -1


def test_projection_round_trip(grid):
    lat, lon = grid.to_latlon(1234.5, 678.9)
    x, y = grid.to_xy(lat, lon)
    assert (x, y) == pytest.approx((1234.5, 678.9))


def test_cell_center_lies_in_its_cell(grid):
    lat, lon = grid.cell_center(6, 13)
    assert grid.cell_of(lat, lon) == (6, 13)
    x, y = grid.to_xy(lat, lon)
    assert (x, y) == pytest.approx((1300, 2700))
