from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import resolve_outbox_path

REPO_ROOT = Path(__file__).resolve().parent.parent
IDENTITIES = json.loads((REPO_ROOT / "test_identities.json").read_text(encoding="utf-8"))
NUMBERS: list[str] = IDENTITIES["numbers"]
IP_PRIMARY: str = IDENTITIES["client_ips"][0]


def _last_code(msisdn: str) -> str:
    rows: list[dict[str, Any]] = []
    for raw in resolve_outbox_path().read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line:
            rows.append(json.loads(line))
    matches = [str(row["code"]) for row in rows if row.get("msisdn") == msisdn]
    assert matches, f"No outbox entry for {msisdn}"
    return matches[-1]


@pytest.mark.asyncio
async def test_20_concurrent_valid_verifies_single_success(
    client: httpx.AsyncClient,
) -> None:
    """20 parallel valid verifies must yield strictly 1 x 200 and 19 x 401."""
    msisdn = NUMBERS[20]
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    code = _last_code(msisdn)

    responses = await asyncio.gather(
        *(
            client.post(
                "/otp/verify",
                json={"msisdn": msisdn, "code": code},
                headers={"X-Forwarded-For": IP_PRIMARY},
            )
            for _ in range(20)
        )
    )
    counts = Counter(r.status_code for r in responses)
    assert counts == {200: 1, 401: 19}, f"Unexpected status counts: {dict(counts)}"


@pytest.mark.asyncio
async def test_50_concurrent_valid_verifies_single_success(
    client: httpx.AsyncClient,
) -> None:
    """50 parallel valid verifies must yield strictly 1 x 200 and 49 x 401 (no 423 or 500)."""
    msisdn = NUMBERS[21]
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    code = _last_code(msisdn)

    # Spread across 5 IPs (10 req/IP) so per-IP 20/min limit does not mask verify atomicity.
    responses = await asyncio.gather(
        *(
            client.post(
                "/otp/verify",
                json={"msisdn": msisdn, "code": code},
                headers={"X-Forwarded-For": f"198.51.100.{10 + (i // 10)}"},
            )
            for i in range(50)
        )
    )
    counts = Counter(r.status_code for r in responses)
    assert 423 not in counts
    assert 500 not in counts
    assert counts == {200: 1, 401: 49}, f"Unexpected status counts: {dict(counts)}"


@pytest.mark.asyncio
async def test_10_concurrent_sends_cooldown_race_safety(
    client: httpx.AsyncClient,
) -> None:
    """10 concurrent /otp/send for the same MSISDN must yield strictly 1 x 200 and 9 x 429."""
    msisdn = NUMBERS[22]

    responses = await asyncio.gather(
        *(client.post("/otp/send", json={"msisdn": msisdn}) for _ in range(10))
    )
    counts = Counter(r.status_code for r in responses)
    assert counts == {200: 1, 429: 9}, f"Unexpected status counts: {dict(counts)}"

    outbox_rows = [
        json.loads(line)
        for line in resolve_outbox_path().read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    msisdn_rows = [row for row in outbox_rows if row.get("msisdn") == msisdn]
    assert len(msisdn_rows) == 1
