from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from infra.hlr import reset_hlr_memory
from infra.telecom_smsc import (
    SmppLoopbackClient,
    SmscState,
    SmsJob,
    _process_otp_job,
    create_gateway_app,
)


@pytest.fixture
def operator_state() -> Iterator[SmscState]:
    """Provide an isolated in-memory telecom operator SmscState (no Postgres/AMQP)."""
    reset_hlr_memory()
    state = SmscState(
        database_url="",
        rabbitmq_url="",
        dlr_callback_url="",
    )
    yield state
    reset_hlr_memory()


@pytest.mark.asyncio
async def test_operator_hlr_mnp_routing_and_absent_subscriber(operator_state: SmscState) -> None:
    """Verify HLR prefix routing, MNP portability, absent subscribers, and MNP updates."""
    gw_app = create_gateway_app(operator_state)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=gw_app),
        base_url="http://operator",
    ) as op_client:
        beeline = await op_client.get("/hlr/lookup", params={"msisdn": "996770730000"})
        assert beeline.status_code == 200
        b_data = beeline.json()
        assert b_data["operator"] == "Beeline KG"
        assert b_data["hlr_status"] == "HLR_OK"
        assert b_data["in_network"] is True
        assert b_data["ported"] is False

        mega = await op_client.get("/hlr/lookup", params={"msisdn": "996550730001"})
        assert mega.status_code == 200
        assert mega.json()["operator"] == "MEGA"

        nur = await op_client.get("/hlr/lookup", params={"msisdn": "996700730002"})
        assert nur.status_code == 200
        assert nur.json()["operator"] == "O!"

        ported = await op_client.get("/hlr/lookup", params={"msisdn": "996770730003"})
        assert ported.status_code == 200
        p_data = ported.json()
        assert p_data["operator"] == "O! [MNP]"
        assert p_data["ported"] is True

        absent = await op_client.get("/hlr/lookup", params={"msisdn": "996770730008"})
        assert absent.status_code == 200
        a_data = absent.json()
        assert a_data["hlr_status"] == "ABSENT_SUBSCRIBER"
        assert a_data["in_network"] is False

        toggle_resp = await op_client.post(
            "/hlr/mnp",
            json={"msisdn": "996770730000", "operator": "MEGA", "in_network": False},
        )
        assert toggle_resp.status_code == 200
        t_data = toggle_resp.json()
        assert t_data["operator"] == "MEGA [MNP]"
        assert t_data["ported"] is True
        assert t_data["hlr_status"] == "ABSENT_SUBSCRIBER"
        assert t_data["in_network"] is False

        after_lookup = await op_client.get("/hlr/lookup", params={"msisdn": "996770730000"})
        assert after_lookup.status_code == 200
        assert after_lookup.json()["operator"] == "MEGA [MNP]"
        assert after_lookup.json()["in_network"] is False

        restore_resp = await op_client.post(
            "/hlr/mnp",
            json={"msisdn": "996770730000", "in_network": True},
        )
        assert restore_resp.status_code == 200
        assert restore_resp.json()["in_network"] is True
        assert restore_resp.json()["hlr_status"] == "HLR_OK"


@pytest.mark.asyncio
async def test_operator_ccn_charging_402_topup_and_atomic_bulk(
    operator_state: SmscState,
) -> None:
    """Reject bulk batch exceeding bulk_mkt balance with 402, top up +500 som, then succeed."""
    gw_app = create_gateway_app(operator_state)
    async with httpx.AsyncClient(
        transport=ASGITransport(app=gw_app),
        base_url="http://operator",
    ) as op_client:
        long_text = "A" * 2600  # 17 GSM-7 segments -> 200 * 17 * 0.30 = 1020.00 som > 1000.00
        overdraft_resp = await op_client.post(
            "/bulk",
            json={"count": 200, "text": long_text},
        )
        assert overdraft_resp.status_code == 402
        overdraft_body = overdraft_resp.json()
        assert overdraft_body["detail"] == "INSUFFICIENT_BALANCE"
        assert overdraft_body["balance"] == 1000.0
        assert overdraft_body["required"] == 1020.0

        status_before = await op_client.get("/billing/status")
        assert status_before.status_code == 200
        before_data = status_before.json()
        assert before_data["balances"]["bulk_mkt"] == 1000.0
        assert len(before_data["cdrs"]) == 0

        topup_resp = await op_client.post(
            "/billing/topup",
            json={"channel": "bulk_mkt", "amount": 500},
        )
        assert topup_resp.status_code == 200
        assert topup_resp.json()["balances"]["bulk_mkt"] == 1500.0

        ok_resp = await op_client.post(
            "/bulk",
            json={"count": 200, "text": long_text},
        )
        assert ok_resp.status_code == 200
        ok_body = ok_resp.json()
        assert ok_body["total_cost"] == 1020.0
        assert ok_body["balance"] == 480.0

        status_after = await op_client.get("/billing/status", params={"limit": 250})
        assert status_after.status_code == 200
        after_data = status_after.json()
        assert after_data["balances"]["bulk_mkt"] == 480.0
        assert len(after_data["cdrs"]) == 200


@pytest.mark.asyncio
async def test_operator_idempotent_charge_and_create_cdr(operator_state: SmscState) -> None:
    """Calling charge_and_create_cdr twice with same msg_id charges and inserts CDR only once."""
    await operator_state.billing.init()
    cdr_first, created_first = await operator_state.billing.charge_and_create_cdr(
        msg_id="otp-idem-001",
        msisdn="996770730000",
        channel="otp_svc",
        text="Your OTP code: 123456",
        operator="Beeline KG",
        hlr_status="HLR_OK",
    )
    assert created_first is True
    assert cdr_first["cost_som"] == 0.45

    cdr_second, created_second = await operator_state.billing.charge_and_create_cdr(
        msg_id="otp-idem-001",
        msisdn="996770730000",
        channel="otp_svc",
        text="Your OTP code: 123456",
        operator="Beeline KG",
        hlr_status="HLR_OK",
    )
    assert created_second is False
    assert cdr_second["msg_id"] == "otp-idem-001"

    snapshot = await operator_state.billing.get_snapshot()
    assert snapshot["balances"]["otp_svc"] == 4999.55
    assert len(snapshot["cdrs"]) == 1


@pytest.mark.asyncio
async def test_operator_expired_otp_dropped_without_charge(operator_state: SmscState) -> None:
    """Expired OTP job creates EXPIRED CDR at 0.00 cost and does not deduct otp_svc balance."""
    await operator_state.billing.init()
    loopback = SmppLoopbackClient(operator_state)
    job = SmsJob(
        msg_id="otp-expired-001",
        msisdn="996770730000",
        text="Your OTP code: 654321",
        channel="otp_svc",
        priority=1,
        validity_period=2,
        expires_at_ms=int(time.time() * 1000) - 5000,
        generation=operator_state.generation,
    )
    async with httpx.AsyncClient() as http_client:
        await _process_otp_job(job, operator_state, loopback, http_client)

    snapshot = await operator_state.billing.get_snapshot()
    assert snapshot["balances"]["otp_svc"] == 5000.0
    assert len(snapshot["cdrs"]) == 1
    cdr = snapshot["cdrs"][0]
    assert cdr["msg_id"] == "otp-expired-001"
    assert cdr["status"] == "EXPIRED"
    assert cdr["cost_som"] == 0.0
    assert operator_state.counters["otp_expired"] == 1


@pytest.mark.asyncio
async def test_operator_absent_subscriber_produces_undeliv_cdr(operator_state: SmscState) -> None:
    """Absent subscriber (996770730008) results in CDR status UNDELIV."""
    await operator_state.billing.init()
    loopback = SmppLoopbackClient(operator_state)
    job = SmsJob(
        msg_id="otp-absent-001",
        msisdn="996770730008",
        text="Your OTP code: 112233",
        channel="otp_svc",
        priority=1,
        validity_period=2,
        expires_at_ms=int(time.time() * 1000) + 60_000,
        generation=operator_state.generation,
    )
    async with httpx.AsyncClient() as http_client:
        await _process_otp_job(job, operator_state, loopback, http_client)

    snapshot = await operator_state.billing.get_snapshot()
    assert len(snapshot["cdrs"]) == 1
    cdr = snapshot["cdrs"][0]
    assert cdr["msg_id"] == "otp-absent-001"
    assert cdr["msisdn"] == "996770730008"
    assert cdr["hlr_status"] == "ABSENT_SUBSCRIBER"
    assert cdr["status"] == "UNDELIV"
    assert operator_state.counters["hlr_absent"] == 1


@pytest.mark.asyncio
async def test_api_boundary_status_standby_when_operator_offline(
    client: httpx.AsyncClient,
) -> None:
    """GET /telecom/status returns 200 STANDBY with empty balances when operator is offline."""
    resp = await client.get("/telecom/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["gateway"]["bound"] == "STANDBY"
    assert body["balances"] == {}
    assert body["storage_engine"] == "STANDBY (Operator Offline)"


@pytest.mark.asyncio
async def test_api_boundary_bulk_and_topup_return_503_when_offline(
    client: httpx.AsyncClient,
) -> None:
    """POST /telecom/bulk and POST /telecom/topup return 503 TELECOM_GATEWAY_OFFLINE."""
    bulk_resp = await client.post("/telecom/bulk", json={"count": 10, "text": "Hello"})
    assert bulk_resp.status_code == 503
    assert bulk_resp.json() == {"detail": "TELECOM_GATEWAY_OFFLINE"}

    topup_resp = await client.post("/telecom/topup", json={"channel": "bulk_mkt", "amount": 500})
    assert topup_resp.status_code == 503
    assert topup_resp.json() == {"detail": "TELECOM_GATEWAY_OFFLINE"}


@pytest.mark.asyncio
async def test_api_boundary_dlr_webhook_does_not_pollute_security_events(
    client: httpx.AsyncClient,
    clean_env: Any,
) -> None:
    """POST /telecom/dlr accepts DLR webhook (200) without writing to Redis security:events."""
    resp = await client.post(
        "/telecom/dlr",
        json={
            "msg_id": "otp-dlr-001",
            "msisdn": "996770730000",
            "status": "DELIVRD",
            "channel": "otp_svc",
            "operator": "Beeline KG",
            "hlr_status": "HLR_OK",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["dlr_status"] == "DELIVRD"

    stream_entries = await clean_env.xrange("security:events", "-", "+")
    assert stream_entries == []
