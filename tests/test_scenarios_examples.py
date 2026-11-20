from __future__ import annotations

import asyncio
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import (
    clear_runtime_overrides,
    resolve_outbox_path,
    set_runtime_overrides,
)
from app.core.clock import clock

REPO_ROOT = Path(__file__).resolve().parent.parent
OPEN_SCENARIOS_PATH = REPO_ROOT / "scenarios_examples.json"
FULL_SCENARIOS_PATH = REPO_ROOT / "tests" / "jury_scenarios_full.json"

OPEN_SCENARIOS: list[dict[str, Any]] = json.loads(
    OPEN_SCENARIOS_PATH.read_text(encoding="utf-8")
)
FULL_SCENARIOS: list[dict[str, Any]] = json.loads(
    FULL_SCENARIOS_PATH.read_text(encoding="utf-8")
)


def _resolve_value(value: Any, bindings: dict[str, str]) -> Any:
    if isinstance(value, str):
        if value.startswith("$wrong:"):
            name = value.removeprefix("$wrong:")
            return f"{(int(bindings[name]) + 1) % 1_000_000:06d}"
        if value.startswith("$"):
            name = value[1:]
            return bindings[name]
    return value


def _resolve_body(body: dict[str, Any], bindings: dict[str, str]) -> dict[str, Any]:
    return {key: _resolve_value(val, bindings) for key, val in body.items()}


def _capture_last_delivery(msisdn: str) -> str:
    outbox_path = resolve_outbox_path()
    assert outbox_path.exists(), f"Outbox file not found at {outbox_path}"

    matched_code: str | None = None
    for raw_line in outbox_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        row = json.loads(line)
        if row.get("msisdn") == msisdn:
            assert isinstance(row.get("ts"), str) and row["ts"], f"Invalid ts in {row}"
            code = str(row["code"])
            assert len(code) == 6 and code.isdigit(), f"Invalid OTP code in {row}"
            matched_code = code

    assert matched_code is not None, f"No outbox delivery found for msisdn={msisdn}"
    return matched_code


def _load_start_ms(fixture_name: str) -> int:
    identities_path = REPO_ROOT / fixture_name
    identities = json.loads(identities_path.read_text(encoding="utf-8"))
    raw_start = str(identities["start_time"]).strip()
    if raw_start.endswith("Z"):
        raw_start = f"{raw_start[:-1]}+00:00"
    dt = datetime.fromisoformat(raw_start)
    return round(dt.timestamp() * 1000)


async def _send_step_request(
    client: httpx.AsyncClient,
    req_spec: dict[str, Any],
    bindings: dict[str, str],
) -> httpx.Response:
    headers: dict[str, str] = {}
    if "client_ip" in req_spec:
        headers["X-Forwarded-For"] = str(req_spec["client_ip"])

    body = _resolve_body(req_spec.get("body", {}), bindings)
    return await client.request(
        method=req_spec["method"],
        url=req_spec["path"],
        json=body,
        headers=headers,
    )


def _assert_no_code_leaks(stats_data: dict[str, Any], bindings: dict[str, str]) -> None:
    if not bindings:
        return
    counters_json = json.dumps(stats_data.get("counters", {}), ensure_ascii=False)
    events = stats_data.get("events", [])
    for code in bindings.values():
        assert code not in counters_json, f"Captured code {code} leaked in security counters"
        for ev in events:
            assert "code" not in ev, f"Key 'code' leaked in security event: {ev}"
            sanitized = {
                k: v
                for k, v in ev.items()
                if k not in ("id", "ts", "msisdn", "client_ip")
            }
            assert code not in json.dumps(sanitized, ensure_ascii=False), (
                f"Captured OTP code {code} leaked in security event: {ev}"
            )


async def _run_scenario(
    client: httpx.AsyncClient,
    scenario: dict[str, Any],
) -> None:
    start_ms = _load_start_ms(scenario.get("fixture", "test_identities.json"))
    clock.freeze(start_ms)
    set_runtime_overrides(scenario.get("overrides"))
    bindings: dict[str, str] = {}

    try:
        for idx, step in enumerate(scenario["steps"]):
            op = step["op"]
            if op == "request":
                resp = await _send_step_request(client, step, bindings)
                expected_status = step["expected"]["status"]
                assert resp.status_code == expected_status, (
                    f"{scenario['id']} step {idx}: expected {expected_status}, "
                    f"got {resp.status_code} ({resp.text})"
                )
                if (
                    step["method"].upper() == "POST"
                    and step["path"] == "/otp/send"
                    and resp.status_code == 200
                ):
                    assert "code" not in resp.json(), (
                        f"{scenario['id']} step {idx}: /otp/send leaked 'code' field"
                    )
            elif op == "capture_delivery":
                bindings[step["bind"]] = _capture_last_delivery(step["msisdn"])
            elif op == "advance":
                clock.advance(float(step["seconds"]))
            elif op == "parallel":
                responses = await asyncio.gather(
                    *(_send_step_request(client, req, bindings) for req in step["requests"])
                )
                expected = step["expected"]
                if "count" in expected:
                    assert len(responses) == expected["count"]
                counts = Counter(str(r.status_code) for r in responses)
                assert counts == expected["status_counts"], (
                    f"{scenario['id']} step {idx}: expected {expected['status_counts']}, "
                    f"got {dict(counts)}"
                )
            else:
                pytest.fail(f"Unsupported scenario op: {op!r}")

        stats_resp = await client.get("/security/stats")
        assert stats_resp.status_code == 200
        _assert_no_code_leaks(stats_resp.json(), bindings)
    finally:
        clear_runtime_overrides()


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", OPEN_SCENARIOS, ids=[s["id"] for s in OPEN_SCENARIOS])
async def test_example_scenario(
    client: httpx.AsyncClient,
    scenario: dict[str, Any],
) -> None:
    """Execute each open jury scenario from scenarios_examples.json end-to-end."""
    await _run_scenario(client, scenario)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", FULL_SCENARIOS, ids=[s["id"] for s in FULL_SCENARIOS])
async def test_jury_scenario_full(
    client: httpx.AsyncClient,
    scenario: dict[str, Any],
) -> None:
    """Execute all 18 full jury scenarios (O01-O18) from tests/jury_scenarios_full.json."""
    await _run_scenario(client, scenario)
