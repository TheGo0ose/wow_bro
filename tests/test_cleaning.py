from datetime import timedelta, timezone

import pytest

from coverage.cleaning import DropReason, load_measurements, parse_number, parse_timestamp
from coverage.errors import DataError

HEADER = "lat,lon,rsrp_dbm,timestamp\n"
GOOD = "42.85,74.58,-95.5,2026-11-16T13:58:30+06:00\n"
TZ = timezone(timedelta(hours=6))


@pytest.mark.parametrize("text, expected", [
    ("-95.5", -95.5), (" -95 ", -95.0), ("1e1", 10.0), ("+.5", 0.5),
])
def test_parse_number_accepts_decimals(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize("text", ["", "NaN", "nan", "Infinity", "-inf", "bad", "1_000", "1e999", "1,5"])
def test_parse_number_rejects_garbage(text):
    assert parse_number(text) is None


def test_parse_timestamp_rejects_impossible_month():
    assert parse_timestamp("2026-13-01T10:00:00+06:00", TZ) is None
    assert parse_timestamp("bad-time", TZ) is None
    assert parse_timestamp("", TZ) is None


def test_parse_timestamp_normalises_offsets():
    a = parse_timestamp("2026-11-16T13:58:30+06:00", TZ)
    b = parse_timestamp("2026-11-16T07:58:30Z", TZ)
    naive = parse_timestamp("2026-11-16T13:58:30", TZ)
    assert a == b == naive


def _clean(write_csv, bounds, body: str):
    return load_measurements(write_csv("m.csv", HEADER + body), bounds)


def test_valid_row_is_kept(write_csv, bounds):
    result = _clean(write_csv, bounds, GOOD)
    assert result.dropped_rows == 0
    assert len(result.measurements) == 1


@pytest.mark.parametrize("row, reason", [
    (",,,\n", DropReason.EMPTY_ROW),
    ("\n", DropReason.EMPTY_ROW),
    ("42.85,74.58,-95.5\n", DropReason.WRONG_COLUMN_COUNT),
    ("42.85,74.58,-95.5,2026-11-16T13:58:30+06:00,extra\n", DropReason.WRONG_COLUMN_COUNT),
    ("42.85,,-95.5,2026-11-16T13:58:30+06:00\n", DropReason.MISSING_VALUE),
    ("42.85,74.58,,2026-11-16T13:58:30+06:00\n", DropReason.MISSING_VALUE),
    ("42.85,74.58,-95.5,\n", DropReason.MISSING_VALUE),
    ("NaN,74.58,-95.5,2026-11-16T13:58:30+06:00\n", DropReason.NOT_A_NUMBER),
    ("42.85,Infinity,-95.5,2026-11-16T13:58:30+06:00\n", DropReason.NOT_A_NUMBER),
    ("42.85,74.58,bad,2026-11-16T13:58:30+06:00\n", DropReason.NOT_A_NUMBER),
    ("42.85,74.58,-95.5,bad-time\n", DropReason.BAD_TIMESTAMP),
    ("42.85,74.58,-95.5,2026-13-01T10:00:00+06:00\n", DropReason.BAD_TIMESTAMP),
    ("42.85,74.58,-140.1,2026-11-16T13:58:30+06:00\n", DropReason.RSRP_OUT_OF_RANGE),
    ("42.85,74.58,-43.9,2026-11-16T13:58:30+06:00\n", DropReason.RSRP_OUT_OF_RANGE),
    ("42.80,74.58,-95.5,2026-11-16T13:58:30+06:00\n", DropReason.OUT_OF_BOUNDS),
    ("42.85,74.70,-95.5,2026-11-16T13:58:30+06:00\n", DropReason.OUT_OF_BOUNDS),
])
def test_garbage_row_is_dropped_with_reason(write_csv, bounds, row, reason):
    result = _clean(write_csv, bounds, GOOD + row)
    assert result.dropped == {reason: 1}
    assert result.dropped_rows == 1
    assert len(result.measurements) == 1


def test_rsrp_range_and_bounds_are_inclusive(write_csv, bounds):
    body = (
        "42.85,74.58,-140,2026-11-16T13:58:30+06:00\n"
        "42.85,74.58,-44,2026-11-16T13:58:30+06:00\n"
        f"{bounds.lat_max},{bounds.lon_max},-95,2026-11-16T13:58:30+06:00\n"
        f"{bounds.lat_min},{bounds.lon_min},-95,2026-11-16T13:58:30+06:00\n"
    )
    assert _clean(write_csv, bounds, body).dropped_rows == 0


def test_duplicates_compare_numbers_as_numbers(write_csv, bounds):
    body = (
        "42.85,74.58,-112.9,2026-11-16T13:58:30+06:00\n"
        "42.850,74.5800,-112.90,2026-11-16T13:58:30+06:00\n"
        "42.85,74.58,-112.9,2026-11-16T07:58:30+00:00\n"  # same instant, other offset
        "42.85,74.58,-112.9,2026-11-16T13:58:31+06:00\n"  # different time -> kept
    )
    result = _clean(write_csv, bounds, body)
    assert result.dropped == {DropReason.DUPLICATE: 2}
    assert len(result.measurements) == 2


def test_invalid_rows_do_not_count_as_originals_for_duplicates(write_csv, bounds):
    body = "42.80,74.58,-95.5,2026-11-16T13:58:30+06:00\n" * 3  # out of bounds x3
    result = _clean(write_csv, bounds, body)
    assert result.dropped == {DropReason.OUT_OF_BOUNDS: 3}


def test_columns_are_found_by_name(write_csv, bounds):
    path = write_csv("m.csv", "timestamp,rsrp_dbm,lon,lat\n2026-11-16T13:58:30+06:00,-95,74.58,42.85\n")
    result = load_measurements(path, bounds)
    assert result.measurements[0].lat == 42.85


def test_broken_bytes_do_not_crash(tmp_path, bounds):
    path = tmp_path / "m.csv"
    path.write_bytes((HEADER + GOOD).encode() + b"42.85,74.58,-9\xff5,2026-11-16T13:58:30+06:00\n")
    result = load_measurements(path, bounds)
    assert result.dropped == {DropReason.NOT_A_NUMBER: 1}


def test_missing_column_is_a_data_error(write_csv, bounds):
    with pytest.raises(DataError):
        load_measurements(write_csv("m.csv", "lat,lon,timestamp\n"), bounds)


def test_missing_file_is_a_data_error(tmp_path, bounds):
    with pytest.raises(DataError):
        load_measurements(tmp_path / "nope.csv", bounds)
