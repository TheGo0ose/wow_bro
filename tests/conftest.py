import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from coverage.config import Bounds  # noqa: E402

DATA_DIR = ROOT / "data"


@pytest.fixture
def bounds() -> Bounds:
    return Bounds(
        lat_min=42.84,
        lat_max=42.876,
        lon_min=74.56,
        lon_max=74.61,
        grid_origin_lat=42.84,
        grid_origin_lon=74.56,
    )


@pytest.fixture
def write_csv(tmp_path):
    def _write(name: str, text: str) -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    return _write
