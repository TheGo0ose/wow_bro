"""Existing towers (towers.csv) and candidate sites (candidates.csv)."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

from coverage.cleaning import parse_number
from coverage.errors import DataError

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Site:
    id: str
    lat: float
    lon: float


def load_sites(path: Path) -> list[Site]:
    """Read a CSV with id,lat,lon. Invalid or repeated rows are skipped with a warning."""
    try:
        handle = open(path, encoding="utf-8-sig", errors="replace", newline="")
    except FileNotFoundError as exc:
        raise DataError(f"sites file not found: {path}") from exc

    sites: list[Site] = []
    seen_ids: set[str] = set()
    with handle:
        reader = csv.DictReader(handle)
        fields = {name.strip().lower() for name in reader.fieldnames or []}
        if not {"id", "lat", "lon"} <= fields:
            raise DataError(f"{path} must have columns id,lat,lon")
        for line_no, raw in enumerate(reader, start=2):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            site_id = row.get("id", "")
            lat, lon = parse_number(row.get("lat", "")), parse_number(row.get("lon", ""))
            if not site_id or lat is None or lon is None:
                log.warning("%s:%d: skipped invalid site row %r", path, line_no, raw)
                continue
            if site_id in seen_ids:
                log.warning("%s:%d: skipped duplicate site id %s", path, line_no, site_id)
                continue
            seen_ids.add(site_id)
            sites.append(Site(id=site_id, lat=lat, lon=lon))
    return sites
