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


@pytest.mark.asyncio
async def test_jury_1000_attempts_attack_zero_breaches(
    client: httpx.AsyncClient,
) -> None:
    """Simulate a 1000-attempt multi-wave jury attack and assert 0 breaches and 0 code leaks."""
    # Wave A (300): Single MSISDN brute-force across 300 rotated IPs; attempt #300 sends real code.
    wave_a_msisdn = "996779900000"
    assert (
        await client.post(
            "/otp/send",
            json={"msisdn": wave_a_msisdn},
            headers={"X-Forwarded-For": "10.200.0.1"},
        )
    ).status_code == 200
    real_code_a = _last_code(wave_a_msisdn)

    statuses_a: list[int] = []
    for i in range(300):
        ip = f"10.200.{i // 250}.{(i % 250) + 1}"
        if i == 299:
            guess = real_code_a
        else:
            cand = (100000 + i) % 1_000_000
            if f"{cand:06d}" == real_code_a:
                cand = (cand + 1) % 1_000_000
            guess = f"{cand:06d}"
        resp = await client.post(
            "/otp/verify",
            json={"msisdn": wave_a_msisdn, "code": guess},
            headers={"X-Forwarded-For": ip},
        )
        statuses_a.append(resp.status_code)

    counts_a = Counter(statuses_a)
    assert counts_a == {401: 4, 423: 296}, f"Unexpected Wave A counts: {dict(counts_a)}"

    stats_a = await client.get("/security/stats", params={"limit": 200})
    assert stats_a.status_code == 200
    assert real_code_a not in json.dumps(stats_a.json().get("events", []))

    # Wave B (350): Single attacker IP spraying 50 MSISDNs (7 passes).
    statuses_b: list[int] = []
    for i in range(350):
        resp = await client.post(
            "/otp/verify",
            json={"msisdn": NUMBERS[i % 50], "code": f"{(200000 + i) % 1_000_000:06d}"},
            headers={"X-Forwarded-For": "198.51.100.250"},
        )
        statuses_b.append(resp.status_code)

    counts_b = Counter(statuses_b)
    assert counts_b == {401: 20, 429: 330}, f"Unexpected Wave B counts: {dict(counts_b)}"

    # Wave C (350): 35-IP distributed botnet attacking 10 active targets concurrently (35/target).
    wave_c_targets = [f"9967799000{k:02d}" for k in range(1, 11)]
    wave_c_codes: list[str] = []
    for target in wave_c_targets:
        assert (
            await client.post(
                "/otp/send",
                json={"msisdn": target},
                headers={"X-Forwarded-For": "10.201.0.254"},
            )
        ).status_code == 200
        wave_c_codes.append(_last_code(target))

    async def fire_bot(idx: int) -> int:
        target_idx = idx % 10
        target_msisdn = wave_c_targets[target_idx]
        target_real_code = wave_c_codes[target_idx]
        cand = (300000 + idx) % 1_000_000
        if f"{cand:06d}" == target_real_code:
            cand = (cand + 1) % 1_000_000
        resp = await client.post(
            "/otp/verify",
            json={"msisdn": target_msisdn, "code": f"{cand:06d}"},
            headers={"X-Forwarded-For": f"10.201.1.{(idx % 35) + 1}"},
        )
        return resp.status_code

    statuses_c = list(await asyncio.gather(*(fire_bot(i) for i in range(350))))
    counts_c = Counter(statuses_c)
    assert counts_c == {401: 40, 423: 310}, f"Unexpected Wave C counts: {dict(counts_c)}"

    total_counts = counts_a + counts_b + counts_c
    assert sum(total_counts.values()) == 1000
    assert total_counts[200] == 0
    assert 500 not in total_counts

    stats_c = await client.get("/security/stats", params={"limit": 200})
    assert stats_c.status_code == 200
    events_c_json = json.dumps(stats_c.json().get("events", []))
    assert real_code_a not in events_c_json
    for code in wave_c_codes:
        assert code not in events_c_json
