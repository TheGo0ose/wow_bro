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
from urllib.parse import urlparse

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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
    enqueued_at: float = field(default_factory=time.monotonic)


class SmscState:
    """Shared state and queues for the SMSC + HTTP gateway."""

    def __init__(self) -> None:
        self.smpp_port: int = int(os.environ.get("SMPP_PORT", "2775"))
        self.http_port: int = int(os.environ.get("GATEWAY_HTTP_PORT", "1401"))
        self.dlr_callback_url: str = os.environ.get(
            "DLR_CALLBACK_URL", "http://otp-api:8000/telecom/dlr"
        )
        self.rabbitmq_url: str = os.environ.get("RABBITMQ_URL", "")
        self.bulk_tps: float = float(os.environ.get("BULK_TPS", "5.0"))

        self.otp_queue: asyncio.Queue[SmsJob] = asyncio.Queue()
        self.bulk_queue: asyncio.Queue[SmsJob] = asyncio.Queue()

        self.bound_sessions: int = 0
        self.loopback_bound: bool = False
        self.counters: dict[str, int] = {
            "otp_enqueued": 0,
            "otp_dispatched": 0,
            "bulk_enqueued": 0,
            "bulk_dispatched": 0,
            "smpp_pdus": 0,
            "smpp_submit_sm": 0,
            "dlr_sent": 0,
            "dlr_failed": 0,
            "amqp_published": 0,
        }

    @property
    def bound_label(self) -> str:
        return "BOUND_TRX" if (self.loopback_bound or self.bound_sessions > 0) else "STANDBY"


class SmppServer:
    """Minimal binary SMPP v3.4 TCP server."""

    def __init__(self, state: SmscState) -> None:
        self.state = state
        self.server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(
            self.handle_client, host="0.0.0.0", port=self.state.smpp_port
        )
        logger.info("SMPP v3.4 TCP server listening on 0.0.0.0:%d", self.state.smpp_port)

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
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
        self.state.loopback_bound = False
        if self._writer is not None:
            with suppress(OSError):
                self._writer.close()
                await self._writer.wait_closed()
            self._writer = None
            self._reader = None


async def publish_amqp_management(
    http_client: httpx.AsyncClient, state: SmscState, job: SmsJob
) -> None:
    """Optionally publish message metadata via RabbitMQ Management HTTP API."""
    if not state.rabbitmq_url:
        return
    try:
        parsed = urlparse(state.rabbitmq_url)
        host = parsed.hostname or "rabbitmq"
        user = parsed.username or "guest"
        password = parsed.password or "guest"
        mgmt_port = os.environ.get("RABBITMQ_MGMT_PORT", "15672")
        url = f"http://{host}:{mgmt_port}/api/exchanges/%2F/amq.default/publish"
        payload = json.dumps(
            {
                "msg_id": job.msg_id,
                "msisdn": job.msisdn,
                "channel": job.channel,
                "priority": job.priority,
            }
        )
        resp = await http_client.post(
            url,
            auth=(user, password),
            json={
                "properties": {"priority": job.priority},
                "routing_key": f"smsc.{job.channel}",
                "payload": payload,
                "payload_encoding": "string",
            },
            timeout=0.5,
        )
        if resp.status_code < 400:
            state.counters["amqp_published"] += 1
    except (httpx.HTTPError, OSError):
        # RabbitMQ is purely optional; never fail SMS dispatch
        return


async def dispatch_job(
    job: SmsJob,
    state: SmscState,
    loopback: SmppLoopbackClient,
    http_client: httpx.AsyncClient,
) -> None:
    """Execute SMPP loopback submit_sm and post DLR callback."""
    try:
        await loopback.submit_sm(job)
    except (OSError, asyncio.IncompleteReadError) as exc:
        logger.warning("SMPP loopback submit_sm error for %s: %s", job.msg_id, exc)

    if job.channel == "otp_svc":
        state.counters["otp_dispatched"] += 1
    else:
        state.counters["bulk_dispatched"] += 1

    try:
        resp = await http_client.post(
            state.dlr_callback_url,
            json={"msg_id": job.msg_id, "msisdn": job.msisdn, "status": "DELIVRD"},
            timeout=1.5,
        )
        if resp.status_code < 400:
            state.counters["dlr_sent"] += 1
        else:
            state.counters["dlr_failed"] += 1
    except (httpx.HTTPError, OSError):
        state.counters["dlr_failed"] += 1


async def otp_worker(
    state: SmscState, loopback: SmppLoopbackClient, http_client: httpx.AsyncClient
) -> None:
    """High-priority worker for otp_svc (immediate dispatch, up to 100 TPS)."""
    while True:
        job = await state.otp_queue.get()
        try:
            await dispatch_job(job, state, loopback, http_client)
        finally:
            state.otp_queue.task_done()


async def bulk_worker(
    state: SmscState, loopback: SmppLoopbackClient, http_client: httpx.AsyncClient
) -> None:
    """Throttled worker for bulk_mkt (~5 TPS, yields to otp_svc)."""
    interval = 1.0 / max(state.bulk_tps, 0.1)
    next_slot = 0.0
    while True:
        job = await state.bulk_queue.get()
        try:
            # Always let pending OTP jobs drain first
            while not state.otp_queue.empty():
                await asyncio.sleep(0.01)

            now = time.monotonic()
            if now < next_slot:
                await asyncio.sleep(next_slot - now)

            while not state.otp_queue.empty():
                await asyncio.sleep(0.01)

            next_slot = time.monotonic() + interval
            await dispatch_job(job, state, loopback, http_client)
        finally:
            state.bulk_queue.task_done()


def create_gateway_app(state: SmscState) -> FastAPI:
    """Create the HTTP gateway FastAPI app managing the SMPP server & workers."""
    smpp_server = SmppServer(state)
    loopback = SmppLoopbackClient(state)
    http_client = httpx.AsyncClient()
    tasks: list[asyncio.Task[Any]] = []
    bg_tasks: set[asyncio.Task[Any]] = set()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await smpp_server.start()
        try:
            await loopback.ensure_bound()
        except (OSError, asyncio.IncompleteReadError) as exc:
            logger.warning("Initial SMPP loopback bind deferred: %s", exc)
        tasks.append(asyncio.create_task(otp_worker(state, loopback, http_client)))
        tasks.append(asyncio.create_task(bulk_worker(state, loopback, http_client)))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for t in list(bg_tasks):
                t.cancel()
            await loopback.close()
            await smpp_server.stop()
            await http_client.aclose()

    gateway = FastAPI(title="OTP-Shield Telecom SMSC Gateway", lifespan=lifespan)

    def _enqueue(item: dict[str, Any], default_channel: str = "otp_svc") -> tuple[SmsJob, str]:
        channel = str(item.get("channel") or default_channel)
        priority = int(item.get("priority", 1 if channel == "otp_svc" else 5))
        job = SmsJob(
            msg_id=str(item.get("msg_id") or uuid.uuid4().hex[:12]),
            msisdn=str(item.get("msisdn") or ""),
            text=str(item.get("text") or ""),
            channel=channel,
            priority=priority,
            validity_period=int(item.get("validity_period", 2)),
        )
        if channel == "otp_svc" or (channel != "bulk_mkt" and priority <= 1):
            state.otp_queue.put_nowait(job)
            state.counters["otp_enqueued"] += 1
            qname = "otp_svc"
        else:
            state.bulk_queue.put_nowait(job)
            state.counters["bulk_enqueued"] += 1
            qname = "bulk_mkt"

        if state.rabbitmq_url:
            t = asyncio.create_task(publish_amqp_management(http_client, state, job))
            bg_tasks.add(t)
            t.add_done_callback(bg_tasks.discard)

        return job, qname

    @gateway.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "smpp_port": state.smpp_port,
            "bound": state.bound_label,
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
            "queues": {
                "otp_svc": state.otp_queue.qsize(),
                "bulk_mkt": state.bulk_queue.qsize(),
            },
            "counters": dict(state.counters),
        }

    @gateway.post("/send")
    async def send_sms(request: Request) -> JSONResponse:
        body = await request.json()
        job, qname = _enqueue(body, default_channel="otp_svc")
        return JSONResponse(
            status_code=200,
            content={"status": "queued", "msg_id": job.msg_id, "queue": qname},
        )

    @gateway.post("/bulk")
    async def send_bulk(request: Request) -> JSONResponse:
        payload = await request.json()
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict) and isinstance(payload.get("messages"), list):
            items = payload["messages"]
        elif isinstance(payload, dict):
            count = int(payload.get("count", 10))
            text = str(payload.get("text", "Promo!"))
            items = [
                {
                    "msg_id": uuid.uuid4().hex[:12],
                    "msisdn": f"99677073{i:04d}",
                    "text": text,
                    "channel": "bulk_mkt",
                    "priority": 5,
                }
                for i in range(count)
            ]
        else:
            return JSONResponse(status_code=422, content={"detail": "Invalid bulk payload"})

        queued_ids: list[str] = []
        for item in items:
            if isinstance(item, dict):
                job, _ = _enqueue(item, default_channel="bulk_mkt")
                queued_ids.append(job.msg_id)
        return JSONResponse(
            status_code=200,
            content={
                "status": "queued",
                "count": len(queued_ids),
                "queue": "bulk_mkt",
                "msg_ids": queued_ids,
            },
        )

    return gateway


def main() -> None:
    state = SmscState()
    gateway_app = create_gateway_app(state)
    uvicorn.run(gateway_app, host="0.0.0.0", port=state.http_port, log_level="info")


if __name__ == "__main__":
    main()
