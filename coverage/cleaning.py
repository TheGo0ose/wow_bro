"""Loading and cleaning of measurements.csv.

Rules (TZ 7 + CLARIFICATIONS, section 7), applied per row in this order:
  1. empty row / wrong number of columns,
  2. required fields present, numbers finite, timestamp is ISO 8601,
  3. RSRP within [-140, -44] dBm,
  4. coordinates inside the district rectangle (borders included),
  5. duplicates by the four normalised fields (numbers compared as numbers,
     timestamps compared as instants).
Every dropped row counts towards dropped_rows, including physically empty ones.
"""

from __future__ import annotations

import csv
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum
from pathlib import Path

from coverage.config import Bounds
from coverage.errors import DataError

log = logging.getLogger(__name__)

REQUIRED_FIELDS = ("lat", "lon", "rsrp_dbm", "timestamp")
RSRP_MIN_DBM = -140.0
RSRP_MAX_DBM = -44.0

# Plain decimal numbers only: rejects "NaN", "Infinity", "1_000", "0x10", empty strings.
_NUMBER_RE = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
# Bishkek has had no DST since 2005, so a fixed offset is a safe fallback.
_BISHKEK_FALLBACK_TZ = timezone(timedelta(hours=6), "Asia/Bishkek")


class DropReason(str, Enum):
    EMPTY_ROW = "empty_row"
    WRONG_COLUMN_COUNT = "wrong_column_count"
    MISSING_VALUE = "missing_value"
    NOT_A_NUMBER = "not_a_number"
    BAD_TIMESTAMP = "bad_timestamp"
    RSRP_OUT_OF_RANGE = "rsrp_out_of_range"
    OUT_OF_BOUNDS = "out_of_bounds"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class Measurement:
    lat: float
    lon: float
    rsrp_dbm: float
    timestamp: datetime

    @property
    def key(self) -> tuple[float, float, float, datetime]:
        """Normalised identity used for de-duplication."""
        return self.lat, self.lon, self.rsrp_dbm, self.timestamp


@dataclass
class CleaningResult:
    measurements: list[Measurement]
    total_rows: int
    dropped: Counter[DropReason] = field(default_factory=Counter)

    @property
    def dropped_rows(self) -> int:
        return sum(self.dropped.values())


def parse_number(text: str) -> float | None:
    """Parse a finite decimal number; return None for anything else."""
    text = text.strip()
    if not _NUMBER_RE.fullmatch(text):
        return None
    value = float(text)
    return value if math.isfinite(value) else None


def parse_timestamp(text: str, default_tz: tzinfo) -> datetime | None:
    """Parse an ISO 8601 timestamp; naive values are interpreted in default_tz."""
    text = text.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=default_tz)
    return value


def resolve_timezone(name: str) -> tzinfo:
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:  # ZoneInfoNotFoundError or missing tzdata on Windows
        log.warning("timezone %r is unavailable, falling back to UTC+06:00", name)
        return _BISHKEK_FALLBACK_TZ


def _check_row(
    row: list[str], column_index: dict[str, int], width: int, bounds: Bounds, tz: tzinfo
) -> Measurement | DropReason:
    if not row or all(not cell.strip() for cell in row):
        return DropReason.EMPTY_ROW
    if len(row) != width:
        return DropReason.WRONG_COLUMN_COUNT

    values = {name: row[column_index[name]].strip() for name in REQUIRED_FIELDS}
    if any(not value for value in values.values()):
        return DropReason.MISSING_VALUE

    lat = parse_number(values["lat"])
    lon = parse_number(values["lon"])
    rsrp = parse_number(values["rsrp_dbm"])
    if lat is None or lon is None or rsrp is None:
        return DropReason.NOT_A_NUMBER

    timestamp = parse_timestamp(values["timestamp"], tz)
    if timestamp is None:
        return DropReason.BAD_TIMESTAMP
    if not RSRP_MIN_DBM <= rsrp <= RSRP_MAX_DBM:
        return DropReason.RSRP_OUT_OF_RANGE
    if not bounds.contains(lat, lon):
        return DropReason.OUT_OF_BOUNDS
    return Measurement(lat=lat, lon=lon, rsrp_dbm=rsrp, timestamp=timestamp)


def _read_header(reader) -> tuple[dict[str, int], int]:
    header = next(reader, None)
    if header is None:
        raise DataError("measurements file is empty (no header)")
    normalised = [name.strip().lower() for name in header]
    missing = [name for name in REQUIRED_FIELDS if name not in normalised]
    if missing:
        raise DataError(f"measurements file has no columns: {', '.join(missing)}")
    return {name: normalised.index(name) for name in REQUIRED_FIELDS}, len(header)


def load_measurements(path: Path, bounds: Bounds) -> CleaningResult:
    """Read measurements.csv, drop invalid rows and duplicates, keep counts by reason."""
    tz = resolve_timezone(bounds.timezone)
    try:
        # errors="replace": a row with broken bytes becomes an invalid row instead of a crash.
        handle = open(path, encoding="utf-8-sig", errors="replace", newline="")
    except FileNotFoundError as exc:
        raise DataError(f"measurements file not found: {path}") from exc

    kept: list[Measurement] = []
    seen: set[tuple[float, float, float, datetime]] = set()
    dropped: Counter[DropReason] = Counter()
    total_rows = 0

    with handle:
        reader = csv.reader(handle)
        column_index, width = _read_header(reader)
        for row in reader:
            total_rows += 1
            outcome = _check_row(row, column_index, width, bounds, tz)
            if isinstance(outcome, DropReason):
                dropped[outcome] += 1
            elif outcome.key in seen:
                dropped[DropReason.DUPLICATE] += 1
            else:
                seen.add(outcome.key)
                kept.append(outcome)

    return CleaningResult(measurements=kept, total_rows=total_rows, dropped=dropped)
