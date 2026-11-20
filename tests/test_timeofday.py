from datetime import datetime, timedelta, timezone

from coverage.cleaning import Measurement
from coverage.geo import LocalGrid
from coverage.timeofday import compare_day_night, is_daytime

BISHKEK = timezone(timedelta(hours=6))
GRID = LocalGrid(origin_lat=42.84, origin_lon=74.56, cell_size_m=200)


def _m(ix, rsrp, hour, minute=0, tz=BISHKEK):
    lat, lon = GRID.cell_center(ix, 0)
    return Measurement(lat, lon, rsrp, datetime(2026, 11, 16, hour, minute, tzinfo=tz))


def test_day_boundaries():
    assert not is_daytime(_m(0, -100, 6, 59), BISHKEK)
    assert is_daytime(_m(0, -100, 7, 0), BISHKEK)
    assert is_daytime(_m(0, -100, 21, 59), BISHKEK)
    assert not is_daytime(_m(0, -100, 22, 0), BISHKEK)


def test_hour_is_taken_in_local_time():
    # 01:30 UTC is 07:30 in Bishkek -> day
    assert is_daytime(_m(0, -100, 1, 30, tz=timezone.utc), BISHKEK)


def test_compare_day_night():
    data = (
        [_m(0, -110, 12, i) for i in range(5)]  # cell 0: red by day
        + [_m(0, -100, 23, i) for i in range(5)]  # cell 0: yellow at night
        + [_m(1, -95, 12, i) for i in range(5)]  # cell 1: only day data
        + [_m(1, -95, 23, i) for i in range(2)]  # too few at night
    )
    result = compare_day_night(data, GRID, BISHKEK)
    assert result.day_samples == 10 and result.night_samples == 7
    assert [c.cell_id for c in result.day.cells] == ["X000_Y000", "X001_Y000"]
    assert [c.cell_id for c in result.night.cells] == ["X000_Y000"]
    assert len(result.common) == 1
    change = result.common[0]
    assert change.delta_db == 10
    assert change.level_shift == 1
    assert result.median_delta_db == 10
    assert len(result.better_at_night) == 1 and not result.worse_at_night


def test_compare_day_night_without_overlap():
    result = compare_day_night([_m(0, -100, 12, i) for i in range(5)], GRID, BISHKEK)
    assert result.common == [] and result.median_delta_db is None
