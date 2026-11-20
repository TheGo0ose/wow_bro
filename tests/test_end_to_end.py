"""The full pipeline on the open data must reproduce the published expected.json."""

import json

from conftest import DATA_DIR

from coverage.cli import main
from coverage.pipeline import InputPaths, run_analysis
from coverage.report import build_result


def test_open_data_matches_expected():
    expected = json.loads((DATA_DIR / "expected.json").read_text(encoding="utf-8"))
    analysis = run_analysis(InputPaths.from_dir(DATA_DIR))
    assert build_result(analysis) == expected


def test_open_data_grid_stats():
    analysis = run_analysis(InputPaths.from_dir(DATA_DIR))
    assert analysis.cleaning.total_rows == 5000
    assert len(analysis.cleaning.measurements) == 4800
    assert len(analysis.grid.cells) + len(analysis.grid.sparse_cells) == 398
    assert len(analysis.grid.sparse_cells) == 11


def test_cli_writes_result_and_map(tmp_path):
    assert main(["--data", str(DATA_DIR), "--out", str(tmp_path)]) == 0
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["best_candidates"] == ["C09", "C14"]
    assert (tmp_path / "coverage_map.html").stat().st_size > 0


def test_cli_reports_missing_data(tmp_path):
    assert main(["--data", str(tmp_path / "missing"), "--out", str(tmp_path)]) == 2
