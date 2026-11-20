from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import resolve_outbox_path
from app.core.clock import clock

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_PATH = REPO_ROOT / "scenarios_examples.json"
SCENARIOS: list[dict[str, Any]] = json.loads(SCENARIOS_PATH.read_text(encoding="utf-8"))


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
            matched_code = str(row["code"])

    assert matched_code is not None, f"No outbox delivery found for msisdn={msisdn}"
    return matched_code


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


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
async def test_example_scenario(
    client: httpx.AsyncClient,
    scenario: dict[str, Any],
) -> None:
    """Execute each open jury scenario from scenarios_examples.json end-to-end."""
    bindings: dict[str, str] = {}

    for idx, step in enumerate(scenario["steps"]):
        op = step["op"]
        if op == "request":
            resp = await _send_step_request(client, step, bindings)
            expected_status = step["expected"]["status"]
            assert resp.status_code == expected_status, (
                f"{scenario['id']} step {idx}: expected {expected_status}, "
                f"got {resp.status_code} ({resp.text})"
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
