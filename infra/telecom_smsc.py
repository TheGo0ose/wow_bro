from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import time
import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from typing import Any

import aio_pika
import aio_pika.exceptions
import asyncpg
import httpx
import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from infra.billing_db import BillingRepository, InsufficientBalanceError
from infra.hlr import apply_hlr_override, lookup_hlr, reset_hlr_memory

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("telecom_smsc")

# SMPP v3.4 Command IDs
CMD_GENERIC_NACK = 0x80000000
CMD_BIND_RECEIVER = 0x00000001
CMD_BIND_TRANSMITTER = 0x00000002
CMD_SUBMIT_SM = 0x00000004
CMD_UNBIND = 0x00000006
CMD_BIND_TRANSCEIVER = 0x00000009
CMD_ENQUIRE_LINK = 0x00000015

ESME_ROK = 0x00000000
ESME_RINVCMDID = 0x00000003

SMPP_HEADER_STRUCT = struct.Struct("!IIII")

_DEMO_BULK_MSISDNS = (
    "996770730000",
    "996550730001",
    "996700730002",
    "996770730003",
    "996550730004",
    "996700730005",
    "996770730006",
    "996770730007",
    "996770730008",
    "996550730009",
)


def _demo_bulk_msisdn(idx: int) -> str:
    if idx < len(_DEMO_BULK_MSISDNS):
        return _DEMO_BULK_MSISDNS[idx]
    return f"99677073{idx % 50:04d}"


def pack_pdu(command_id: int, status: int, seq: int, body: bytes = b"") -> bytes:
    """Pack a binary SMPP v3.4 PDU with 16-byte header."""
    length = 16 + len(body)
    return SMPP_HEADER_STRUCT.pack(length, command_id, status, seq) + body


def build_submit_sm_body(msisdn: str, text: str, channel: str) -> bytes:
    """Build a minimal SMPP v3.4 submit_sm body."""
    service_type = b"\x00"
    source_addr = channel.encode("ascii", errors="ignore")[:20] + b"\x00"
    dest_addr = msisdn.encode("ascii", errors="ignore")[:20] + b"\x00"
    short_msg = text.encode("utf-8", errors="ignore")[:254]
    return (
        service_type
        + bytes([5, 0])  # source_addr_ton, source_addr_npi
        + source_addr
        + bytes([1, 1])  # dest_addr_ton, dest_addr_npi
        + dest_addr
        + bytes([0, 0, 0])  # esm_class, protocol_id, priority_flag
        + b"\x00\x00"  # schedule_delivery_time, validity_period
        + bytes([1, 0, 0, 0])  # registered_delivery, replace, data_coding, sm_default_msg_id
        + bytes([len(short_msg)])
        + short_msg
    )


@dataclass
class SmsJob:
    """Queued SMS job for SMPP dispatch."""

    msg_id: str
    msisdn: str
    text: str
    channel: str
    priority: int
    validity_period: int
    expires_at_ms: int | None = None
    generation: int = 0
    enqueued_at: float = field(default_factory=time.monotonic)


class SmscState:
    """Shared state, billing repository, and queues for the SMSC + HTTP gateway."""

    def __init__(
        self,
        *,
        database_url: str | None = None,
        rabbitmq_url: str | None = None,
        dlr_callback_url: str | None = None,
        smpp_port: int | None = None,
        http_port: int | None = None,
        bulk_tps: float | None = None,
    ) -> None:
        self.smpp_port: int = (
            int(os.environ.get("SMPP_PORT", "2775")) if smpp_port is None else smpp_port
        )
        self.http_port: int = (
            int(os.environ.get("GATEWAY_HTTP_PORT", "1401")) if http_port is None else http_port
        )
        self.dlr_callback_url: str = (
            os.environ.get("DLR_CALLBACK_URL", "http://otp-api:8000/telecom/dlr")
            if dlr_callback_url is None
            else dlr_callback_url
        )
        self.rabbitmq_url: str = (
            os.environ.get("RABBITMQ_URL", "") if rabbitmq_url is None else rabbitmq_url
        ).strip()
        self.database_url: str = (
            os.environ.get("DATABASE_URL", "") if database_url is None else database_url
        ).strip()
        self.bulk_tps: float = (
            float(os.environ.get("BULK_TPS", "5.0")) if bulk_tps is None else bulk_tps
        )

        self.generation: int = 0
        self.otp_inflight: int = 0
        self.amqp_connected: bool = False
        self.billing: BillingRepository = BillingRepository(database_url=self.database_url)

        self.otp_queue: asyncio.Queue[SmsJob] = asyncio.Queue()
        self.bulk_queue: asyncio.Queue[SmsJob] = asyncio.Queue()

        self.bound_sessions: int = 0
        self.loopback_bound: bool = False
        self.counters: dict[str, int] = {
            "otp_enqueued": 0,
            "otp_dispatched": 0,
            "otp_expired": 0,
            "otp_rejected": 0,
            "bulk_enqueued": 0,
            "bulk_dispatched": 0,
            "hlr_absent": 0,
            "smpp_pdus": 0,
            "smpp_submit_sm": 0,
            "dlr_sent": 0,
            "dlr_failed": 0,
            "amqp_consumed": 0,
        }

    def reset_counters(self) -> None:
        """Zero all gateway telemetry counters."""
        for key in self.counters:
            self.counters[key] = 0

    @property
    def bound_label(self) -> str:
        """Return SMPP session bind state label."""
        return "BOUND_TRX" if (self.loopback_bound or self.bound_sessions > 0) else "STANDBY"


class SmppServer:
    """Minimal binary SMPP v3.4 TCP server."""

    def __init__(self, state: SmscState) -> None:
        self.state = state
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        """Start listening for binary SMPP v3.4 TCP connections."""
        self.server = await asyncio.start_server(
            self.handle_client, host="0.0.0.0", port=self.state.smpp_port
        )
        logger.info("SMPP v3.4 TCP server listening on 0.0.0.0:%d", self.state.smpp_port)

    async def stop(self) -> None:
        """Stop the SMPP TCP server."""
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle an inbound SMPP v3.4 TCP session."""
        is_bound = False
        try:
            while True:
                try:
                    header_bytes = await reader.readexactly(16)
                except asyncio.IncompleteReadError:
                    break

                cmd_len, cmd_id, _cmd_status, seq = SMPP_HEADER_STRUCT.unpack(header_bytes)
                if cmd_len < 16 or cmd_len > 65536:
                    writer.write(pack_pdu(CMD_GENERIC_NACK, ESME_RINVCMDID, seq))
                    await writer.drain()
                    break

                body_len = cmd_len - 16
                if body_len > 0:
                    await reader.readexactly(body_len)

                self.state.counters["smpp_pdus"] += 1

                if cmd_id in (CMD_BIND_TRANSMITTER, CMD_BIND_RECEIVER, CMD_BIND_TRANSCEIVER):
                    if not is_bound:
                        is_bound = True
                        self.state.bound_sessions += 1
                    resp = pack_pdu(cmd_id | 0x80000000, ESME_ROK, seq, b"OTP-SMSC\x00")
                    writer.write(resp)
                    await writer.drain()
                elif cmd_id == CMD_ENQUIRE_LINK:
                    writer.write(pack_pdu(cmd_id | 0x80000000, ESME_ROK, seq))
                    await writer.drain()
                elif cmd_id == CMD_SUBMIT_SM:
                    self.state.counters["smpp_submit_sm"] += 1
                    smsc_msg_id = uuid.uuid4().hex[:12].encode("ascii") + b"\x00"
                    writer.write(pack_pdu(cmd_id | 0x80000000, ESME_ROK, seq, smsc_msg_id))
                    await writer.drain()
                elif cmd_id == CMD_UNBIND:
                    writer.write(pack_pdu(cmd_id | 0x80000000, ESME_ROK, seq))
                    await writer.drain()
                    break
                else:
                    writer.write(pack_pdu(CMD_GENERIC_NACK, ESME_RINVCMDID, seq))
                    await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            if is_bound:
                self.state.bound_sessions = max(0, self.state.bound_sessions - 1)
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()


class SmppLoopbackClient:
    """Persistent local SMPP v3.4 transceiver loopback client."""

    def __init__(self, state: SmscState) -> None:
        self.state = state
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._seq: int = 0
        self._lock = asyncio.Lock()

    def _next_seq(self) -> int:
        self._seq = (self._seq % 0x7FFFFFFF) + 1
        return self._seq

    async def ensure_bound(self) -> None:
        """Ensure an active SMPP bind_transceiver session to the local SMSC."""
        if self._writer is not None and not self._writer.is_closing():
            return
        self._reader, self._writer = await asyncio.open_connection(
            "127.0.0.1", self.state.smpp_port
        )
        bind_body = b"otp_gw\x00password\x00SMPP\x00" + bytes([0x34, 0, 0]) + b"\x00"
        seq = self._next_seq()
        self._writer.write(pack_pdu(CMD_BIND_TRANSCEIVER, ESME_ROK, seq, bind_body))
        await self._writer.drain()
        await self._read_pdu()
        self.state.loopback_bound = True
        logger.info("SMPP loopback transceiver bound to 127.0.0.1:%d", self.state.smpp_port)

    async def _read_pdu(self) -> tuple[int, int, int, bytes]:
        assert self._reader is not None
        header = await self._reader.readexactly(16)
        cmd_len, cmd_id, status, seq = SMPP_HEADER_STRUCT.unpack(header)
        body = b""
        if cmd_len > 16:
            body = await self._reader.readexactly(cmd_len - 16)
        return cmd_id, status, seq, body

    async def submit_sm(self, job: SmsJob) -> None:
        """Send a binary SMPP submit_sm PDU for an SMS job."""
        async with self._lock:
            for attempt in range(2):
                try:
                    await self.ensure_bound()
                    assert self._writer is not None
                    body = build_submit_sm_body(job.msisdn, job.text, job.channel)
                    seq = self._next_seq()
                    self._writer.write(pack_pdu(CMD_SUBMIT_SM, ESME_ROK, seq, body))
                    await self._writer.drain()
                    await self._read_pdu()
                    return
                except (OSError, asyncio.IncompleteReadError):
                    self.state.loopback_bound = False
                    await self.close()
                    if attempt == 1:
                        raise

    async def close(self) -> None:
        """Close the loopback TCP connection."""
        self.state.loopback_bound = False
        if self._writer is not None:
            with suppress(OSError):
                self._writer.close()
                await self._writer.wait_closed()
            self._writer = None
            self._reader = None


class BulkThrottle:
    """Rate limiter enforcing bulk_mkt TPS while yielding to OTP traffic."""

    def __init__(self, state: SmscState) -> None:
        self.state = state
        self._lock = asyncio.Lock()
        self._next_slot: float = 0.0

    async def wait_for_slot(self) -> None:
        """Block until OTP queues/inflight are idle and the 5 TPS slot arrives."""
        interval = 1.0 / max(self.state.bulk_tps, 0.1)
        async with self._lock:
            while self.state.otp_inflight > 0 or not self.state.otp_queue.empty():
                await asyncio.sleep(0.01)

            now = time.monotonic()
            if now < self._next_slot:
                await asyncio.sleep(self._next_slot - now)

            while self.state.otp_inflight > 0 or not self.state.otp_queue.empty():
                await asyncio.sleep(0.01)

            self._next_slot = time.monotonic() + interval


def _job_from_payload(
    item: dict[str, Any],
    *,
    default_channel: str,
    generation: int,
) -> SmsJob:
    channel = str(item.get("channel") or default_channel)
    default_prio = 1 if channel == "otp_svc" else 5
    raw_exp = item.get("expires_at_ms")
    expires_at_ms = int(raw_exp) if raw_exp is not None else None
    raw_gen = item.get("generation")
    job_gen = int(raw_gen) if raw_gen is not None else generation
    return SmsJob(
        msg_id=str(item.get("msg_id") or uuid.uuid4().hex[:12]),
        msisdn=str(item.get("msisdn") or ""),
        text=str(item.get("text") or ""),
        channel=channel,
        priority=int(item.get("priority", default_prio)),
        validity_period=int(item.get("validity_period", 2)),
        expires_at_ms=expires_at_ms,
        generation=job_gen,
    )


async def _post_dlr(
    http_client: httpx.AsyncClient,
    state: SmscState,
    *,
    msg_id: str,
    msisdn: str,
    channel: str,
    operator: str,
    hlr_status: str,
    status: str,
) -> None:
    if not state.dlr_callback_url:
        return
    try:
        resp = await http_client.post(
            state.dlr_callback_url,
            json={
                "msg_id": msg_id,
                "msisdn": msisdn,
                "channel": channel,
                "operator": operator,
                "hlr_status": hlr_status,
                "status": status,
            },
            timeout=1.5,
        )
        if resp.status_code < 400:
            state.counters["dlr_sent"] += 1
        else:
            state.counters["dlr_failed"] += 1
    except (httpx.HTTPError, OSError, RuntimeError):
        state.counters["dlr_failed"] += 1


async def _process_otp_job(
    job: SmsJob,
    state: SmscState,
    loopback: SmppLoopbackClient,
    http_client: httpx.AsyncClient,
) -> None:
    """Execute expiry check, HLR lookup, SDP charging, SMPP submit_sm, and DLR callback."""
    if job.generation != state.generation:
        return

    state.otp_inflight += 1
    try:
        if job.expires_at_ms is not None and int(time.time() * 1000) >= job.expires_at_ms:
            hlr = lookup_hlr(job.msisdn)
            await state.billing.charge_and_create_cdr(
                msg_id=job.msg_id,
                msisdn=job.msisdn,
                channel="otp_svc",
                text=job.text,
                operator=hlr.operator,
                hlr_status=hlr.hlr_status,
                status="EXPIRED",
                enforce_balance=False,
                cost_override=0.0,
            )
            state.counters["otp_expired"] += 1
            if job.generation == state.generation:
                await _post_dlr(
                    http_client,
                    state,
                    msg_id=job.msg_id,
                    msisdn=job.msisdn,
                    channel="otp_svc",
                    operator=hlr.operator,
                    hlr_status=hlr.hlr_status,
                    status="EXPIRED",
                )
            return

        hlr = lookup_hlr(job.msisdn)
        try:
            _, created = await state.billing.charge_and_create_cdr(
                msg_id=job.msg_id,
                msisdn=job.msisdn,
                channel="otp_svc",
                text=job.text,
                operator=hlr.operator,
                hlr_status=hlr.hlr_status,
                status="SUBMITTED",
            )
        except InsufficientBalanceError:
            await state.billing.charge_and_create_cdr(
                msg_id=job.msg_id,
                msisdn=job.msisdn,
                channel="otp_svc",
                text=job.text,
                operator=hlr.operator,
                hlr_status=hlr.hlr_status,
                status="REJECTED",
                enforce_balance=False,
                cost_override=0.0,
            )
            state.counters["otp_rejected"] += 1
            if job.generation == state.generation:
                await _post_dlr(
                    http_client,
                    state,
                    msg_id=job.msg_id,
                    msisdn=job.msisdn,
                    channel="otp_svc",
                    operator=hlr.operator,
                    hlr_status=hlr.hlr_status,
                    status="UNDELIV",
                )
            return

        if not created:
            return

        if hlr.in_network:
            try:
                await loopback.submit_sm(job)
            except (OSError, asyncio.IncompleteReadError) as exc:
                logger.warning("SMPP loopback submit_sm error for %s: %s", job.msg_id, exc)
            final_status = "DELIVRD"
        else:
            state.counters["hlr_absent"] += 1
            final_status = "UNDELIV"

        state.counters["otp_dispatched"] += 1
        if job.generation != state.generation:
            return

        await state.billing.update_cdr_status(
            msg_id=job.msg_id,
            status=final_status,
            operator=hlr.operator,
            hlr_status=hlr.hlr_status,
        )
        await _post_dlr(
            http_client,
            state,
            msg_id=job.msg_id,
            msisdn=job.msisdn,
            channel="otp_svc",
            operator=hlr.operator,
            hlr_status=hlr.hlr_status,
            status=final_status,
        )
    finally:
        state.otp_inflight = max(0, state.otp_inflight - 1)


async def process_otp_payload(
    payload: dict[str, Any],
    state: SmscState,
    loopback: SmppLoopbackClient,
    http_client: httpx.AsyncClient,
) -> None:
    """Parse an OTP payload dictionary and execute OTP dispatch pipeline."""
    job = _job_from_payload(payload, default_channel="otp_svc", generation=state.generation)
    await _process_otp_job(job, state, loopback, http_client)


async def _process_bulk_job(
    job: SmsJob,
    state: SmscState,
    loopback: SmppLoopbackClient,
    http_client: httpx.AsyncClient,
) -> None:
    if job.generation != state.generation:
        return

    hlr = lookup_hlr(job.msisdn)
    if hlr.in_network:
        try:
            await loopback.submit_sm(job)
        except (OSError, asyncio.IncompleteReadError) as exc:
            logger.warning("SMPP loopback bulk submit_sm error for %s: %s", job.msg_id, exc)
        final_status = "DELIVRD"
    else:
        state.counters["hlr_absent"] += 1
        final_status = "UNDELIV"

    state.counters["bulk_dispatched"] += 1
    if job.generation != state.generation:
        return

    await state.billing.update_cdr_status(
        msg_id=job.msg_id,
        status=final_status,
        operator=hlr.operator,
        hlr_status=hlr.hlr_status,
    )
    await _post_dlr(
        http_client,
        state,
        msg_id=job.msg_id,
        msisdn=job.msisdn,
        channel="bulk_mkt",
        operator=hlr.operator,
        hlr_status=hlr.hlr_status,
        status=final_status,
    )


async def otp_worker(
    state: SmscState, loopback: SmppLoopbackClient, http_client: httpx.AsyncClient
) -> None:
    """High-priority local fallback worker for otp_svc."""
    while True:
        job = await state.otp_queue.get()
        try:
            await _process_otp_job(job, state, loopback, http_client)
        except (asyncpg.PostgresError, httpx.HTTPError, OSError, RuntimeError, ValueError):
            logger.exception("Unhandled error in otp_worker for %s", job.msg_id)
        finally:
            state.otp_queue.task_done()


async def bulk_worker(
    state: SmscState,
    loopback: SmppLoopbackClient,
    http_client: httpx.AsyncClient,
    throttle: BulkThrottle,
) -> None:
    """Throttled local fallback worker for bulk_mkt (~5 TPS, yields to otp_svc)."""
    while True:
        job = await state.bulk_queue.get()
        try:
            if job.generation != state.generation:
                continue
            await throttle.wait_for_slot()
            await _process_bulk_job(job, state, loopback, http_client)
        except (asyncpg.PostgresError, httpx.HTTPError, OSError, RuntimeError, ValueError):
            logger.exception("Unhandled error in bulk_worker for %s", job.msg_id)
        finally:
            state.bulk_queue.task_done()


def _drain_queue(q: asyncio.Queue[SmsJob]) -> None:
    while True:
        try:
            q.get_nowait()
            q.task_done()
        except asyncio.QueueEmpty:
            break


def create_gateway_app(state: SmscState) -> FastAPI:
    """Create the HTTP gateway FastAPI app managing SMPP, AMQP, HLR, and SDP billing."""
    smpp_server = SmppServer(state)
    loopback = SmppLoopbackClient(state)
    http_client = httpx.AsyncClient()
    throttle = BulkThrottle(state)
    tasks: list[asyncio.Task[Any]] = []

    amqp_resources: dict[str, Any] = {
        "connection": None,
        "pub_channel": None,
        "otp_queue": None,
        "bulk_queue": None,
    }

    async def _on_otp_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process():
            payload = json.loads(message.body.decode("utf-8"))
            state.counters["amqp_consumed"] += 1
            await process_otp_payload(payload, state, loopback, http_client)

    async def _on_bulk_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
        async with message.process():
            payload = json.loads(message.body.decode("utf-8"))
            state.counters["amqp_consumed"] += 1
            job = _job_from_payload(
                payload, default_channel="bulk_mkt", generation=state.generation
            )
            if job.generation != state.generation:
                return
            await throttle.wait_for_slot()
            await _process_bulk_job(job, state, loopback, http_client)

    async def _connect_amqp_once() -> bool:
        if not state.rabbitmq_url:
            return False
        conn = await aio_pika.connect_robust(state.rabbitmq_url)
        otp_ch = await conn.channel()
        await otp_ch.set_qos(prefetch_count=20)
        otp_q = await otp_ch.declare_queue("smsc.otp_svc", durable=True)

        bulk_ch = await conn.channel()
        await bulk_ch.set_qos(prefetch_count=5)
        bulk_q = await bulk_ch.declare_queue("smsc.bulk_mkt", durable=True)

        pub_ch = await conn.channel()

        await otp_q.consume(_on_otp_message)
        await bulk_q.consume(_on_bulk_message)

        amqp_resources["connection"] = conn
        amqp_resources["pub_channel"] = pub_ch
        amqp_resources["otp_queue"] = otp_q
        amqp_resources["bulk_queue"] = bulk_q
        state.amqp_connected = True
        logger.info("Connected to RabbitMQ AMQP and consuming smsc.otp_svc / smsc.bulk_mkt")
        return True

    async def _amqp_connect_loop() -> None:
        while not state.amqp_connected:
            try:
                if await _connect_amqp_once():
                    return
            except asyncio.CancelledError:
                raise
            except (aio_pika.exceptions.AMQPError, OSError, RuntimeError) as exc:
                logger.warning("Waiting for RabbitMQ AMQP connection: %s", exc)
                await asyncio.sleep(2.0)

    async def _publish_amqp(routing_key: str, payload: dict[str, Any]) -> bool:
        pub_ch = amqp_resources.get("pub_channel")
        if not state.amqp_connected or pub_ch is None or pub_ch.is_closed:
            return False
        try:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            msg = aio_pika.Message(
                body=body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            )
            await pub_ch.default_exchange.publish(msg, routing_key=routing_key)
            return True
        except (aio_pika.exceptions.AMQPError, OSError, RuntimeError) as exc:
            logger.warning("AMQP publish to %s failed, using local queue: %s", routing_key, exc)
            return False

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        for attempt in range(5):
            try:
                await state.billing.init()
                break
            except (asyncpg.PostgresError, OSError, RuntimeError) as exc:
                if attempt == 4:
                    logger.error("Billing repository init failed: %s", exc)
                    raise
                logger.warning("Waiting for PostgreSQL init (attempt %d): %s", attempt + 1, exc)
                await asyncio.sleep(1.5)

        await smpp_server.start()
        try:
            await loopback.ensure_bound()
        except (OSError, asyncio.IncompleteReadError) as exc:
            logger.warning("Initial SMPP loopback bind deferred: %s", exc)

        tasks.append(asyncio.create_task(otp_worker(state, loopback, http_client)))
        tasks.append(asyncio.create_task(bulk_worker(state, loopback, http_client, throttle)))

        if state.rabbitmq_url:
            try:
                await _connect_amqp_once()
            except (aio_pika.exceptions.AMQPError, OSError, RuntimeError) as exc:
                logger.warning("Initial AMQP connect deferred, retrying in background: %s", exc)
                tasks.append(asyncio.create_task(_amqp_connect_loop()))

        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            conn = amqp_resources.get("connection")
            if conn is not None:
                with suppress(aio_pika.exceptions.AMQPError, OSError, RuntimeError):
                    await conn.close()
            state.amqp_connected = False
            await loopback.close()
            await smpp_server.stop()
            await http_client.aclose()
            await state.billing.close()

    gateway = FastAPI(title="OTP-Shield Telecom SMSC Gateway", lifespan=lifespan)

    @gateway.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "smpp_port": state.smpp_port,
            "bound": state.bound_label,
            "amqp_connected": state.amqp_connected,
            "queues": {
                "otp_svc": state.otp_queue.qsize(),
                "bulk_mkt": state.bulk_queue.qsize(),
            },
        }

    @gateway.get("/status")
    async def status() -> dict[str, Any]:
        return {
            "status": "ok",
            "smpp_port": state.smpp_port,
            "bound": state.bound_label,
            "bound_sessions": state.bound_sessions,
            "amqp_connected": state.amqp_connected,
            "queues": {
                "otp_svc": state.otp_queue.qsize(),
                "bulk_mkt": state.bulk_queue.qsize(),
            },
            "counters": dict(state.counters),
        }

    @gateway.get("/billing/status")
    async def billing_status(limit: int = Query(default=50, ge=1, le=500)) -> dict[str, Any]:
        snapshot = await state.billing.get_snapshot(limit=limit)
        return {
            "gateway": {
                "enabled": True,
                "online": True,
                "bound": state.bound_label,
                "smpp_port": state.smpp_port,
                "amqp_connected": state.amqp_connected,
                "counters": dict(state.counters),
            },
            **snapshot,
        }

    @gateway.get("/hlr/lookup")
    async def hlr_lookup(msisdn: str) -> dict[str, Any]:
        return lookup_hlr(msisdn).to_dict()

    @gateway.post("/hlr/mnp")
    async def hlr_mnp(request: Request) -> JSONResponse:
        body = await request.json()
        msisdn = str(body.get("msisdn") or "")
        operator = body.get("operator")
        in_network = body.get("in_network")
        try:
            result = apply_hlr_override(
                msisdn=msisdn,
                operator=operator if isinstance(operator, str) else None,
                in_network=bool(in_network) if in_network is not None else None,
            )
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})

        await state.billing.save_hlr_override(
            msisdn=result.msisdn,
            operator=operator if isinstance(operator, str) else None,
            in_network=bool(in_network) if in_network is not None else None,
        )
        return JSONResponse(status_code=200, content=result.to_dict())

    @gateway.post("/send")
    async def send_sms(request: Request) -> JSONResponse:
        body = await request.json()
        channel = str(body.get("channel") or "otp_svc")
        job = _job_from_payload(body, default_channel=channel, generation=state.generation)
        qname = "bulk_mkt" if job.channel == "bulk_mkt" else "otp_svc"
        payload = {
            "msg_id": job.msg_id,
            "msisdn": job.msisdn,
            "text": job.text,
            "channel": qname,
            "priority": job.priority,
            "validity_period": job.validity_period,
            "expires_at_ms": job.expires_at_ms,
            "generation": job.generation,
        }
        published = await _publish_amqp(f"smsc.{qname}", payload)
        if not published:
            if qname == "otp_svc":
                state.otp_queue.put_nowait(job)
            else:
                state.bulk_queue.put_nowait(job)

        if qname == "otp_svc":
            state.counters["otp_enqueued"] += 1
        else:
            state.counters["bulk_enqueued"] += 1

        return JSONResponse(
            status_code=200,
            content={"status": "queued", "msg_id": job.msg_id, "queue": qname},
        )

    @gateway.post("/bulk")
    async def send_bulk(request: Request) -> JSONResponse:
        payload = await request.json()
        raw_items: list[dict[str, Any]]
        batch_text: str

        if isinstance(payload, list):
            raw_items = [item for item in payload if isinstance(item, dict)]
            batch_text = str(raw_items[0].get("text", "Promo!")) if raw_items else "Promo!"
        elif isinstance(payload, dict) and isinstance(payload.get("messages"), list):
            raw_items = [item for item in payload["messages"] if isinstance(item, dict)]
            batch_text = str(
                payload.get("text")
                or (raw_items[0].get("text") if raw_items else "")
                or "Promo!"
            )
        elif isinstance(payload, dict):
            count = int(payload.get("count", 10))
            if count < 1 or count > 200:
                return JSONResponse(
                    status_code=422, content={"detail": "count must be between 1 and 200"}
                )
            batch_text = str(payload.get("text", "Akciya! Skidki 20% tolko segodnya!"))
            raw_items = [
                {
                    "msg_id": f"bulk-{uuid.uuid4().hex[:12]}",
                    "msisdn": _demo_bulk_msisdn(idx),
                    "text": batch_text,
                    "channel": "bulk_mkt",
                    "priority": 5,
                }
                for idx in range(count)
            ]
        else:
            return JSONResponse(status_code=422, content={"detail": "Invalid bulk payload"})

        charge_items: list[dict[str, Any]] = []
        for idx, item in enumerate(raw_items):
            msisdn = str(item.get("msisdn") or _demo_bulk_msisdn(idx))
            hlr = lookup_hlr(msisdn)
            charge_items.append(
                {
                    "msg_id": str(item.get("msg_id") or f"bulk-{uuid.uuid4().hex[:12]}"),
                    "msisdn": msisdn,
                    "text": str(item.get("text") or batch_text),
                    "operator": hlr.operator,
                    "hlr_status": hlr.hlr_status,
                    "status": "SUBMITTED",
                }
            )

        try:
            summary = await state.billing.charge_bulk_batch(items=charge_items, text=batch_text)
        except InsufficientBalanceError as exc:
            return JSONResponse(
                status_code=402,
                content={
                    "detail": "INSUFFICIENT_BALANCE",
                    "channel": exc.channel,
                    "balance": exc.balance,
                    "required": exc.required,
                },
            )

        gen = state.generation
        msg_ids: list[str] = []
        for item in charge_items:
            msg_ids.append(item["msg_id"])
            amqp_payload = {
                "msg_id": item["msg_id"],
                "msisdn": item["msisdn"],
                "text": item["text"],
                "channel": "bulk_mkt",
                "priority": 5,
                "validity_period": 60,
                "generation": gen,
            }
            published = await _publish_amqp("smsc.bulk_mkt", amqp_payload)
            if not published:
                state.bulk_queue.put_nowait(
                    _job_from_payload(amqp_payload, default_channel="bulk_mkt", generation=gen)
                )
            state.counters["bulk_enqueued"] += 1

        return JSONResponse(
            status_code=200,
            content={
                "status": "queued",
                "count": len(msg_ids),
                "channel": "bulk_mkt",
                "encoding": summary["encoding"],
                "segments": summary["segments"],
                "total_cost": summary["total_cost"],
                "balance": summary["balance"],
                "msg_ids": msg_ids,
            },
        )

    @gateway.post("/billing/topup")
    async def billing_topup(request: Request) -> JSONResponse:
        body = await request.json()
        channel = str(body.get("channel") or "bulk_mkt")
        raw_amount = body.get("amount", body.get("amount_som"))
        if raw_amount is None:
            return JSONResponse(status_code=422, content={"detail": "amount is required"})
        try:
            amount = float(raw_amount)
            balances = await state.billing.topup_balance(channel=channel, amount_som=amount)
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "channel": channel,
                "amount": round(amount, 2),
                "balances": balances,
            },
        )

    @gateway.post("/_internal/reset")
    async def internal_reset() -> dict[str, Any]:
        state.generation += 1
        _drain_queue(state.otp_queue)
        _drain_queue(state.bulk_queue)

        for key in ("otp_queue", "bulk_queue"):
            amqp_q = amqp_resources.get(key)
            if amqp_q is not None and state.amqp_connected:
                with suppress(aio_pika.exceptions.AMQPError, OSError, RuntimeError):
                    await amqp_q.purge()

        await state.billing.reset_all()
        reset_hlr_memory()
        state.reset_counters()
        return {"status": "ok", "generation": state.generation}

    return gateway


def main() -> None:
    """Run the telecom SMSC + HTTP gateway process."""
    state = SmscState()
    gateway_app = create_gateway_app(state)
    uvicorn.run(gateway_app, host="0.0.0.0", port=state.http_port, log_level="info")


if __name__ == "__main__":
    main()
