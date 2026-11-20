from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import get_settings, resolve_outbox_path
from app.core.clock import clock

REPO_ROOT = Path(__file__).resolve().parent.parent
IDENTITIES = json.loads((REPO_ROOT / "test_identities.json").read_text(encoding="utf-8"))
NUMBERS: list[str] = IDENTITIES["numbers"]
IP_PRIMARY: str = IDENTITIES["client_ips"][0]
IP_SECONDARY: str = IDENTITIES["client_ips"][1]


def _read_outbox_rows() -> list[dict[str, Any]]:
    path = resolve_outbox_path()
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _last_code(msisdn: str) -> str:
    matches = [row["code"] for row in _read_outbox_rows() if row.get("msisdn") == msisdn]
    assert matches, f"Expected outbox entry for {msisdn}"
    return str(matches[-1])


def _wrong_code(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


@pytest.mark.asyncio
async def test_send_and_verify_success_and_outbox_format(
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue OTP, confirm code never leaks in HTTP response, verify outbox JSONL & override."""
    custom_outbox = tmp_path / "nested" / "custom_sms.jsonl"
    monkeypatch.setenv("OTP_OUTBOX", str(custom_outbox))
    msisdn = NUMBERS[0]

    send_resp = await client.post("/otp/send", json={"msisdn": msisdn})
    assert send_resp.status_code == 200
    payload = send_resp.json()
    assert "code" not in payload
    assert payload["msisdn"] == msisdn

    rows = _read_outbox_rows()
    assert len(rows) == 1
    row = rows[0]
    assert set(row.keys()) == {"ts", "msisdn", "code"}
    assert row["msisdn"] == msisdn
    assert len(row["code"]) == 6 and row["code"].isdigit()
    # Confirm ISO-8601 timestamp parses cleanly.
    parsed_ts = datetime.fromisoformat(row["ts"])
    assert parsed_ts.utcoffset() is not None
    assert row["code"] not in send_resp.text

    verify_resp = await client.post(
        "/otp/verify",
        json={"msisdn": msisdn, "code": row["code"]},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert verify_resp.status_code == 200
    assert verify_resp.json()["status"] == "verified"


@pytest.mark.asyncio
async def test_wrong_code_returns_401_then_valid_succeeds(client: httpx.AsyncClient) -> None:
    """Wrong code returns 401 without blocking subsequent valid verification."""
    msisdn = NUMBERS[1]
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    code = _last_code(msisdn)

    bad_resp = await client.post(
        "/otp/verify",
        json={"msisdn": msisdn, "code": _wrong_code(code)},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert bad_resp.status_code == 401

    ok_resp = await client.post(
        "/otp/verify",
        json={"msisdn": msisdn, "code": code},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert ok_resp.status_code == 200


@pytest.mark.asyncio
async def test_verify_without_active_otp_returns_401(client: httpx.AsyncClient) -> None:
    """Verifying a number that has never requested an OTP returns 401."""
    resp = await client.post(
        "/otp/verify",
        json={"msisdn": NUMBERS[2], "code": "123456"},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_boundary_expiry_valid_at_119_999s(client: httpx.AsyncClient) -> None:
    """OTP remains valid right before the 120s boundary (119.999s)."""
    msisdn = NUMBERS[3]
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    code = _last_code(msisdn)

    clock.advance(119.999)
    resp = await client.post(
        "/otp/verify",
        json={"msisdn": msisdn, "code": code},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_boundary_expiry_expired_at_120_0s(client: httpx.AsyncClient) -> None:
    """OTP expires at exactly 120.0s and returns 410 Gone."""
    msisdn = NUMBERS[4]
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    code = _last_code(msisdn)

    clock.advance(120.0)
    resp = await client.post(
        "/otp/verify",
        json={"msisdn": msisdn, "code": code},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert resp.status_code == 410


@pytest.mark.asyncio
async def test_reuse_verified_code_returns_401_without_lockout(
    client: httpx.AsyncClient,
) -> None:
    """Reusing an already verified code returns 401 and must never trigger 423 lockout."""
    msisdn = NUMBERS[5]
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    code = _last_code(msisdn)

    first = await client.post(
        "/otp/verify",
        json={"msisdn": msisdn, "code": code},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert first.status_code == 200

    for _ in range(7):
        replay = await client.post(
            "/otp/verify",
            json={"msisdn": msisdn, "code": code},
            headers={"X-Forwarded-For": IP_PRIMARY},
        )
        assert replay.status_code == 401


@pytest.mark.asyncio
async def test_resend_after_30s_invalidates_previous_code(client: httpx.AsyncClient) -> None:
    """Issuing a new OTP after cooldown invalidates the prior code."""
    msisdn = NUMBERS[6]
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    c1 = _last_code(msisdn)

    clock.advance(30.0)
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    c2 = _last_code(msisdn)

    if c1 != c2:
        old_resp = await client.post(
            "/otp/verify",
            json={"msisdn": msisdn, "code": c1},
            headers={"X-Forwarded-For": IP_PRIMARY},
        )
        assert old_resp.status_code == 401

    new_resp = await client.post(
        "/otp/verify",
        json={"msisdn": msisdn, "code": c2},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert new_resp.status_code == 200


@pytest.mark.asyncio
async def test_resend_does_not_reset_fail_counter(client: httpx.AsyncClient) -> None:
    """Calling /otp/send must NOT reset fail:{msisdn}; 5 total failures still lock."""
    msisdn = NUMBERS[7]
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    c1 = _last_code(msisdn)

    # 3 wrong attempts before resend
    for _ in range(3):
        r = await client.post(
            "/otp/verify",
            json={"msisdn": msisdn, "code": _wrong_code(c1)},
            headers={"X-Forwarded-For": IP_PRIMARY},
        )
        assert r.status_code == 401

    clock.advance(30.0)
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    c2 = _last_code(msisdn)

    # 4th wrong attempt -> 401
    r4 = await client.post(
        "/otp/verify",
        json={"msisdn": msisdn, "code": _wrong_code(c2)},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert r4.status_code == 401

    # 5th wrong attempt -> 423 Locked (proving fail counter survived /otp/send)
    r5 = await client.post(
        "/otp/verify",
        json={"msisdn": msisdn, "code": _wrong_code(c2)},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert r5.status_code == 423
    assert "Retry-After" in r5.headers


@pytest.mark.asyncio
async def test_progressive_cooldowns_30_60_and_c1_remains_valid(
    client: httpx.AsyncClient,
) -> None:
    """Enforce 30s and 60s progressive cooldowns with Retry-After; blocked send preserves code."""
    msisdn = NUMBERS[8]

    # 1st send at t=0
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    c1 = _last_code(msisdn)

    # At 29.999s -> 429 + Retry-After, and $c1 remains valid
    clock.advance(29.999)
    blocked_1 = await client.post("/otp/send", json={"msisdn": msisdn})
    assert blocked_1.status_code == 429
    assert int(blocked_1.headers["Retry-After"]) >= 1

    verify_c1 = await client.post(
        "/otp/verify",
        json={"msisdn": msisdn, "code": c1},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert verify_c1.status_code == 200

    # At 30.0s -> 2nd send allowed
    clock.advance(0.001)
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200

    # At 89.999s (59.999s after 2nd send) -> 429 + Retry-After
    clock.advance(59.999)
    blocked_2 = await client.post("/otp/send", json={"msisdn": msisdn})
    assert blocked_2.status_code == 429
    assert int(blocked_2.headers["Retry-After"]) >= 1

    # At 90.0s (60.0s after 2nd send) -> 3rd send allowed
    clock.advance(0.001)
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200


@pytest.mark.asyncio
async def test_progressive_cooldown_120s_tier(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the 3rd progressive cooldown tier (120s) when 10m send cap permits a 4th send."""
    monkeypatch.setenv("SEND_LIMIT_10M", "5")
    get_settings.cache_clear()
    msisdn = NUMBERS[9]

    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    clock.advance(30.0)
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    clock.advance(60.0)
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200

    # 119.999s after 3rd send -> 429
    clock.advance(119.999)
    blocked_3 = await client.post("/otp/send", json={"msisdn": msisdn})
    assert blocked_3.status_code == 429
    assert int(blocked_3.headers["Retry-After"]) >= 1

    # 120.0s after 3rd send -> 200
    clock.advance(0.001)
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200


@pytest.mark.asyncio
async def test_10_minute_send_limit_and_recovery_at_600s(client: httpx.AsyncClient) -> None:
    """Allow 3 sends in 10 minutes, reject 4th with 429 + Retry-After, recover at 600.0s."""
    msisdn = NUMBERS[10]

    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    clock.advance(30.0)
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    clock.advance(60.0)
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200

    # Even after 120s cooldown (total 210s < 600s), 4th send hits 10-minute cap
    clock.advance(120.0)
    capped = await client.post("/otp/send", json={"msisdn": msisdn})
    assert capped.status_code == 429
    assert int(capped.headers["Retry-After"]) >= 1

    # Just before 600.0s from first send -> still 429
    clock.advance(389.999)
    still_capped = await client.post("/otp/send", json={"msisdn": msisdn})
    assert still_capped.status_code == 429

    # At exactly 600.0s from first send -> oldest entry evicted, send succeeds
    clock.advance(0.001)
    recovered = await client.post("/otp/send", json={"msisdn": msisdn})
    assert recovered.status_code == 200


@pytest.mark.asyncio
async def test_five_wrong_attempts_lock_and_unlock_at_900s(client: httpx.AsyncClient) -> None:
    """5 wrong attempts lock MSISDN for 900s on both verify and send, then unlock cleanly."""
    msisdn = NUMBERS[11]
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    code = _last_code(msisdn)

    for _ in range(4):
        resp = await client.post(
            "/otp/verify",
            json={"msisdn": msisdn, "code": _wrong_code(code)},
            headers={"X-Forwarded-For": IP_PRIMARY},
        )
        assert resp.status_code == 401

    lock_resp = await client.post(
        "/otp/verify",
        json={"msisdn": msisdn, "code": _wrong_code(code)},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert lock_resp.status_code == 423
    assert int(lock_resp.headers["Retry-After"]) == 900

    # Locked for both /otp/verify (even right code) and /otp/send
    verify_locked = await client.post(
        "/otp/verify",
        json={"msisdn": msisdn, "code": code},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert verify_locked.status_code == 423
    assert "Retry-After" in verify_locked.headers

    send_locked = await client.post("/otp/send", json={"msisdn": msisdn})
    assert send_locked.status_code == 423
    assert "Retry-After" in send_locked.headers

    # Still locked at 899.999s
    clock.advance(899.999)
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 423

    # Unlocks at 900.0s and fail counter is reset
    clock.advance(0.001)
    assert (await client.post("/otp/send", json={"msisdn": msisdn})).status_code == 200
    new_code = _last_code(msisdn)

    # 4 more wrong attempts must return 401 (not immediate 423), 5th re-locks
    for _ in range(4):
        r = await client.post(
            "/otp/verify",
            json={"msisdn": msisdn, "code": _wrong_code(new_code)},
            headers={"X-Forwarded-For": IP_SECONDARY},
        )
        assert r.status_code == 401

    relock = await client.post(
        "/otp/verify",
        json={"msisdn": msisdn, "code": _wrong_code(new_code)},
        headers={"X-Forwarded-For": IP_SECONDARY},
    )
    assert relock.status_code == 423


@pytest.mark.asyncio
async def test_ip_rate_limit_isolation_and_window_reset(client: httpx.AsyncClient) -> None:
    """Allow 20 verifies/60s per IP, block 21st with 429, isolate 2nd IP, reset at 60.0s."""
    # Issue one valid OTP on a dedicated number for verifying after unblock/on 2nd IP
    target_msisdn = NUMBERS[30]
    assert (await client.post("/otp/send", json={"msisdn": target_msisdn})).status_code == 200
    target_code = _last_code(target_msisdn)

    # Burn 20 verifies from IP_PRIMARY across 20 distinct MSISDNs
    for i in range(20):
        resp = await client.post(
            "/otp/verify",
            json={"msisdn": NUMBERS[i], "code": "000000"},
            headers={"X-Forwarded-For": IP_PRIMARY},
        )
        assert resp.status_code == 401

    # 21st verify from IP_PRIMARY -> 429 + Retry-After
    blocked = await client.post(
        "/otp/verify",
        json={"msisdn": target_msisdn, "code": target_code},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1

    # Secondary IP is unaffected and can verify validly -> 200
    other_ip_resp = await client.post(
        "/otp/verify",
        json={"msisdn": target_msisdn, "code": target_code},
        headers={"X-Forwarded-For": IP_SECONDARY},
    )
    assert other_ip_resp.status_code == 200

    # Prepare another OTP to test IP_PRIMARY recovery at 60.0s
    recovery_msisdn = NUMBERS[31]
    assert (await client.post("/otp/send", json={"msisdn": recovery_msisdn})).status_code == 200
    recovery_code = _last_code(recovery_msisdn)

    clock.advance(59.999)
    still_blocked = await client.post(
        "/otp/verify",
        json={"msisdn": recovery_msisdn, "code": recovery_code},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert still_blocked.status_code == 429

    clock.advance(0.001)
    unblocked = await client.post(
        "/otp/verify",
        json={"msisdn": recovery_msisdn, "code": recovery_code},
        headers={"X-Forwarded-For": IP_PRIMARY},
    )
    assert unblocked.status_code == 200


@pytest.mark.asyncio
async def test_audit_log_records_events_without_leaking_otp_codes(
    client: httpx.AsyncClient,
    clean_env: Any,
) -> None:
    """Verify Redis Stream security:events and /security/stats record events with zero OTP codes."""
    msisdn = NUMBERS[12]
    send_resp = await client.post("/otp/send", json={"msisdn": msisdn})
    assert send_resp.status_code == 200
    c1 = _last_code(msisdn)

    # Generate failed, locked, and rate-limited events
    for _ in range(5):
        await client.post(
            "/otp/verify",
            json={"msisdn": msisdn, "code": _wrong_code(c1)},
            headers={"X-Forwarded-For": IP_PRIMARY},
        )
    await client.post("/otp/send", json={"msisdn": msisdn})

    # Issue a second OTP on another number and verify it
    msisdn2 = NUMBERS[13]
    send_resp2 = await client.post("/otp/send", json={"msisdn": msisdn2})
    assert send_resp2.status_code == 200
    c2 = _last_code(msisdn2)
    assert (
        await client.post(
            "/otp/verify",
            json={"msisdn": msisdn2, "code": c2},
            headers={"X-Forwarded-For": IP_PRIMARY},
        )
    ).status_code == 200

    stream_entries = await clean_env.xrange("security:events", "-", "+")
    assert len(stream_entries) >= 8

    recorded_events = [fields["event"] for _, fields in stream_entries]
    assert "OTP_SENT" in recorded_events
    assert "OTP_FAILED" in recorded_events
    assert "MSISDN_LOCKED" in recorded_events
    assert "OTP_VERIFIED" in recorded_events

    for _, fields in stream_entries:
        serialized = json.dumps(fields, ensure_ascii=False)
        assert c1 not in serialized
        assert c2 not in serialized

    stats_resp = await client.get("/security/stats")
    assert stats_resp.status_code == 200
    stats_body = stats_resp.json()
    assert stats_body["successful_breaches"] == 0
    assert stats_body["active_locks"] >= 1
    assert c1 not in stats_resp.text
    assert c2 not in stats_resp.text
    assert c1 not in send_resp.text
    assert c2 not in send_resp2.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/otp/send", {}),
        ("/otp/send", {"msisdn": ""}),
        ("/otp/send", {"msisdn": "123456"}),
        ("/otp/send", {"msisdn": "1" * 16}),
        ("/otp/send", {"msisdn": "+996770730000"}),
        ("/otp/send", {"msisdn": "99677073000a"}),
        ("/otp/verify", {"msisdn": "bad", "code": "123456"}),
        ("/otp/verify", {"msisdn": NUMBERS[0], "code": ""}),
        ("/otp/verify", {"msisdn": NUMBERS[0], "code": "12345"}),
        ("/otp/verify", {"msisdn": NUMBERS[0], "code": "1234567"}),
        ("/otp/verify", {"msisdn": NUMBERS[0], "code": "12345a"}),
        ("/otp/verify", {"msisdn": NUMBERS[0], "code": 123456}),
    ],
)
async def test_invalid_inputs_return_422_never_500(
    client: httpx.AsyncClient,
    path: str,
    payload: dict[str, Any],
) -> None:
    """Malformed or schema-invalid payloads must return 422, never 500."""
    resp = await client.post(path, json=payload)
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_malformed_json_returns_422_never_500(client: httpx.AsyncClient) -> None:
    """Raw invalid JSON request body must return 422, never 500."""
    resp = await client.post(
        "/otp/send",
        content=b"{not-valid-json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_health_endpoint_ok(client: httpx.AsyncClient) -> None:
    """Verify /health reports Redis up and returns 200."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["redis"] == "up"


@pytest.mark.asyncio
async def test_hlr_mnp_routing_and_absent_subscriber(client: httpx.AsyncClient) -> None:
    """Verify HLR prefix routing, MNP portability, absent subscribers, and MNP updates."""
    beeline = await client.get("/telecom/hlr", params={"msisdn": "996770730000"})
    assert beeline.status_code == 200
    b_data = beeline.json()
    assert b_data["operator"] == "Beeline KG"
    assert b_data["hlr_status"] == "HLR_OK"
    assert b_data["in_network"] is True
    assert b_data["ported"] is False

    mega = await client.get("/telecom/hlr", params={"msisdn": "996550730001"})
    assert mega.status_code == 200
    assert mega.json()["operator"] == "MEGA"

    nur = await client.get("/telecom/hlr", params={"msisdn": "996700730002"})
    assert nur.status_code == 200
    assert nur.json()["operator"] == "O!"

    ported = await client.get("/telecom/hlr", params={"msisdn": "996770730003"})
    assert ported.status_code == 200
    p_data = ported.json()
    assert p_data["operator"] == "O! [MNP]"
    assert p_data["ported"] is True

    absent = await client.get("/telecom/hlr", params={"msisdn": "996770730008"})
    assert absent.status_code == 200
    a_data = absent.json()
    assert a_data["hlr_status"] == "ABSENT_SUBSCRIBER"
    assert a_data["in_network"] is False

    toggle_resp = await client.post(
        "/telecom/hlr/mnp",
        json={"msisdn": "996770730000", "operator": "MEGA", "in_network": False},
    )
    assert toggle_resp.status_code == 200
    t_data = toggle_resp.json()
    assert t_data["operator"] == "MEGA [MNP]"
    assert t_data["ported"] is True
    assert t_data["hlr_status"] == "ABSENT_SUBSCRIBER"
    assert t_data["in_network"] is False

    after_lookup = await client.get("/telecom/hlr", params={"msisdn": "996770730000"})
    assert after_lookup.status_code == 200
    assert after_lookup.json()["operator"] == "MEGA [MNP]"
    assert after_lookup.json()["in_network"] is False

    restore_resp = await client.post(
        "/telecom/hlr/mnp",
        json={"msisdn": "996770730000", "in_network": True},
    )
    assert restore_resp.status_code == 200
    assert restore_resp.json()["in_network"] is True
    assert restore_resp.json()["hlr_status"] == "HLR_OK"


@pytest.mark.asyncio
async def test_ccn_charging_402_insufficient_balance_and_topup(client: httpx.AsyncClient) -> None:
    """Reject bulk batch exceeding bulk_mkt balance with 402, top up +500 som, then succeed."""
    long_text = "A" * 2600  # 17 GSM-7 segments -> 200 * 17 * 0.30 = 1020.00 som > 1000.00 som
    overdraft_resp = await client.post(
        "/telecom/bulk",
        json={"count": 200, "text": long_text},
    )
    assert overdraft_resp.status_code == 402
    assert overdraft_resp.json()["detail"] == "INSUFFICIENT_BALANCE"

    status_before = await client.get("/telecom/status")
    assert status_before.status_code == 200
    assert status_before.json()["balances"]["bulk_mkt"] == 1000.0

    topup_resp = await client.post(
        "/telecom/topup",
        json={"channel": "bulk_mkt", "amount": 500},
    )
    assert topup_resp.status_code == 200
    assert topup_resp.json()["balances"]["bulk_mkt"] == 1500.0

    ok_resp = await client.post(
        "/telecom/bulk",
        json={"count": 200, "text": long_text},
    )
    assert ok_resp.status_code == 200

    status_after = await client.get("/telecom/status")
    assert status_after.status_code == 200
    assert status_after.json()["balances"]["bulk_mkt"] == 480.0
