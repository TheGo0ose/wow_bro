"""Сценарии в формате жюри по настоящему HTTP: 5 открытых + собственные."""

from __future__ import annotations

from pathlib import Path

import pytest

from scenario_runner import load_scenarios, run_scenario

SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"
ALL = load_scenarios([SCENARIOS_DIR / "scenarios_examples.json",
                      SCENARIOS_DIR / "extra_scenarios.json"])


@pytest.mark.parametrize("scenario", ALL, ids=[s["id"] for s in ALL])
def test_scenario(live_server, scenario):
    result = run_scenario(live_server["base_url"], live_server["outbox"], scenario)
    assert result.passed, "\n".join(result.failures)


def test_security_log_has_no_codes(live_server):
    """После всех сценариев ни один выданный код не попал в журнал безопасности."""
    import json

    codes = {json.loads(l)["code"] for l in live_server["outbox"].read_text("utf-8").splitlines() if l}
    log_text = live_server["security_log"].read_text("utf-8")
    assert codes, "сценарии должны были выдать коды"
    for line in log_text.splitlines():
        record = json.loads(line)
        assert "code" not in record
        assert not codes.intersection(str(v) for v in record.values()), record
