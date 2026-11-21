from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from typing import Any
from urllib.parse import urlparse

import aio_pika
import httpx

from app.config import get_settings
from app.core.clock import clock

logger = logging.getLogger("otp_shield.telecom")
_DNS_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="telecom-dns")


def _telecom_enabled() -> str:
    raw = os.environ.get("TELECOM_ENABLED")
    if raw is None:
        raw = get_settings().TELECOM_ENABLED
    return raw.strip().lower()


def _telecom_gateway_url() -> str:
    raw = os.environ.get("TELECOM_GATEWAY_URL")
    if raw is None:
        raw = get_settings().TELECOM_GATEWAY_URL
    return raw.strip().rstrip("/")


def _rabbitmq_url() -> str:
    raw = os.environ.get("RABBITMQ_URL")
    if raw is None:
        raw = get_settings().RABBITMQ_URL
    return raw.strip()


class SmsDispatcher:
    """Async telecom SMS gateway dispatcher and thin HTTP/AMQP boundary."""

    def __init__(self) -> None:
        self._telecom_online: bool = False
        self._circuit_open_until: float = 0.0
        self._probe_in_flight: bool = False
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._http_client: httpx.AsyncClient | None = None
        self._http_loop: asyncio.AbstractEventLoop | None = None
        self._amqp_connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._amqp_channel: aio_pika.abc.AbstractChannel | None = None
        self._amqp_loop: asyncio.AbstractEventLoop | None = None
        self._amqp_lock: asyncio.Lock | None = None
        self._amqp_lock_loop: asyncio.AbstractEventLoop | None = None

    def _get_http_client(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        if (
            self._http_client is None
            or self._http_client.is_closed
            or self._http_loop is not loop
        ):
            self._http_client = httpx.AsyncClient()
            self._http_loop = loop
        return self._http_client

    def _get_amqp_lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._amqp_lock is None or self._amqp_lock_loop is not loop:
            self._amqp_lock = asyncio.Lock()
            self._amqp_lock_loop = loop
        return self._amqp_lock

    async def _close_amqp(self) -> None:
        channel = self._amqp_channel
        conn = self._amqp_connection
        amqp_loop = self._amqp_loop
        self._amqp_channel = None
        self._amqp_connection = None
        self._amqp_loop = None
        with suppress(RuntimeError):
            if amqp_loop is not asyncio.get_running_loop():
                return
        if channel is not None and not channel.is_closed:
            with suppress(Exception):
                await channel.close()
        if conn is not None and not conn.is_closed:
            with suppress(Exception):
                await conn.close()

    async def probe_gateway(self) -> bool:
        """Probe telecom gateway /health without blocking FastAPI/AnyIO thread pools."""
        if _telecom_enabled() == "false":
            self._telecom_online = False
            return False
        if self._probe_in_flight:
            return self._telecom_online

        self._probe_in_flight = True
        url = f"{_telecom_gateway_url()}/health"
        try:
            parsed = urlparse(url)
            host = parsed.hostname
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            if host and host not in ("localhost", "127.0.0.1", "::1"):
                loop = asyncio.get_running_loop()
                await asyncio.wait_for(
                    loop.run_in_executor(_DNS_EXECUTOR, socket.getaddrinfo, host, port),
                    timeout=0.35,
                )

            client = self._get_http_client()
            resp = await client.get(url, timeout=0.4)
            if resp.status_code == 200:
                self._telecom_online = True
                self._circuit_open_until = 0.0
                return True
        except (TimeoutError, httpx.HTTPError, OSError) as exc:
            logger.debug("Telecom gateway probe failed: %s", type(exc).__name__)
        finally:
            self._probe_in_flight = False

        self._mark_offline()
        return False

    def enqueue_otp_sms(
        self,
        redis_client: Any = None,
        *,
        msisdn: str,
        code: str,
    ) -> None:
        """Schedule OTP dispatch in background if telecom gateway is online."""
        del redis_client
        if _telecom_enabled() == "false" or not self._telecom_online:
            return
        if time.monotonic() < self._circuit_open_until:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        task = loop.create_task(
            self._send_otp_background(msisdn=msisdn, code=code)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _publish_otp_amqp(self, payload: dict[str, Any], ttl_seconds: int) -> bool:
        rabbitmq_url = _rabbitmq_url()
        if not rabbitmq_url or _telecom_enabled() == "false" or not self._telecom_online:
            return False

        try:
            lock = self._get_amqp_lock()
            async with lock:
                loop = asyncio.get_running_loop()
                if (
                    self._amqp_connection is None
                    or self._amqp_connection.is_closed
                    or self._amqp_loop is not loop
                ):
                    if self._amqp_connection is not None:
                        await self._close_amqp()
                    self._amqp_connection = await aio_pika.connect_robust(
                        rabbitmq_url,
                        timeout=2.0,
                    )
                    self._amqp_channel = await self._amqp_connection.channel()
                    await self._amqp_channel.declare_queue("smsc.otp_svc", durable=True)
                    self._amqp_loop = loop
                elif self._amqp_channel is None or self._amqp_channel.is_closed:
                    self._amqp_channel = await self._amqp_connection.channel()
                    await self._amqp_channel.declare_queue("smsc.otp_svc", durable=True)

                message = aio_pika.Message(
                    body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    expiration=max(1, int(ttl_seconds)),
                )
                await self._amqp_channel.default_exchange.publish(
                    message,
                    routing_key="smsc.otp_svc",
                )
                return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "AMQP OTP publish failed, falling back to HTTP: %s",
                type(exc).__name__,
            )
            await self._close_amqp()
            return False

    async def _send_otp_background(self, *, msisdn: str, code: str) -> None:
        ttl_seconds = max(1, int(get_settings().OTP_TTL))
        payload: dict[str, Any] = {
            "msg_id": f"otp-{uuid.uuid4().hex[:12]}",
            "msisdn": msisdn,
            "text": f"Your OTP code: {code}",
            "channel": "otp_svc",
            "priority": 1,
            "validity_period": 2,
            "expires_at_ms": clock.now_ms() + ttl_seconds * 1000,
        }

        if _rabbitmq_url() and await self._publish_otp_amqp(payload, ttl_seconds):
            return

        url = f"{_telecom_gateway_url()}/send"
        try:
            client = self._get_http_client()
            resp = await client.post(url, json=payload, timeout=2.0)
            if resp.status_code >= 400 and resp.status_code != 402:
                self._mark_offline()
        except (httpx.HTTPError, OSError):
            self._mark_offline()

    def _mark_offline(self) -> None:
        self._telecom_online = False
        self._circuit_open_until = time.monotonic() + 10.0

    def _standby_snapshot(self) -> dict[str, Any]:
        return {
            "gateway": self.get_status(),
            "storage_engine": "STANDBY (Operator Offline)",
            "balances": {},
            "cdrs": [],
            "hlr": {},
        }

    async def get_telecom_snapshot(self, limit: int = 50) -> dict[str, Any]:
        """Query operator billing/HLR snapshot or return standby snapshot if offline."""
        if not self._telecom_online or _telecom_enabled() == "false":
            return self._standby_snapshot()

        url = f"{_telecom_gateway_url()}/billing/status"
        try:
            client = self._get_http_client()
            resp = await client.get(url, params={"limit": limit}, timeout=1.5)
            if resp.status_code >= 400:
                self._mark_offline()
                return self._standby_snapshot()
            data = resp.json()
            if not isinstance(data, dict):
                return self._standby_snapshot()
            result = dict(data)
            gateway_info = self.get_status()
            if isinstance(result.get("gateway"), dict):
                gateway_info = {**gateway_info, **result["gateway"]}
            result["gateway"] = gateway_info
            result.setdefault("storage_engine", "PostgreSQL 18 (SDP-07 ACID)")
            result.setdefault("balances", {})
            result.setdefault("cdrs", [])
            result.setdefault("hlr", {})
            return result
        except (httpx.HTTPError, OSError, ValueError):
            self._mark_offline()
            return self._standby_snapshot()

    async def _proxy_request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Forward a telecom API request to the operator gateway, preserving status codes."""
        if _telecom_enabled() == "false":
            return (503, {"detail": "TELECOM_GATEWAY_OFFLINE"})

        if not self._telecom_online:
            if time.monotonic() < self._circuit_open_until:
                return (503, {"detail": "TELECOM_GATEWAY_OFFLINE"})
            await self.probe_gateway()
        if not self._telecom_online:
            return (503, {"detail": "TELECOM_GATEWAY_OFFLINE"})

        url = f"{_telecom_gateway_url()}{path}"
        try:
            client = self._get_http_client()
            resp = await client.request(
                method,
                url,
                json=json_body,
                params=params,
                timeout=3.0,
            )
            try:
                body = resp.json()
            except ValueError:
                body = {"detail": resp.text or "INVALID_OPERATOR_RESPONSE"}
            if not isinstance(body, dict):
                body = {"result": body}
            return (resp.status_code, body)
        except (httpx.HTTPError, OSError):
            self._mark_offline()
            return (503, {"detail": "TELECOM_GATEWAY_OFFLINE"})

    async def proxy_bulk(self, count: int, text: str) -> tuple[int, dict[str, Any]]:
        """Proxy bulk SMS dispatch to the telecom operator gateway."""
        return await self._proxy_request(
            "POST",
            "/bulk",
            json_body={"count": count, "text": text},
        )

    async def proxy_topup(self, channel: str, amount: float) -> tuple[int, dict[str, Any]]:
        """Proxy channel balance top-up to the telecom operator gateway."""
        return await self._proxy_request(
            "POST",
            "/billing/topup",
            json_body={"channel": channel, "amount": amount},
        )

    async def proxy_hlr_lookup(self, msisdn: str) -> tuple[int, dict[str, Any]]:
        """Proxy HLR/MNP lookup to the telecom operator gateway."""
        return await self._proxy_request(
            "GET",
            "/hlr/lookup",
            params={"msisdn": msisdn},
        )

    async def proxy_hlr_mnp(
        self,
        msisdn: str,
        operator: str | None = None,
        in_network: bool | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """Proxy MNP/presence update to the telecom operator gateway."""
        return await self._proxy_request(
            "POST",
            "/hlr/mnp",
            json_body={
                "msisdn": msisdn,
                "operator": operator,
                "in_network": in_network,
            },
        )

    async def notify_reset(self) -> None:
        """Notify the telecom operator gateway to reset state during test resets."""
        if _telecom_enabled() == "false" or not self._telecom_online:
            return
        with suppress(Exception):
            client = self._get_http_client()
            await client.post(f"{_telecom_gateway_url()}/_internal/reset", timeout=1.0)

    async def close(self) -> None:
        """Cancel background tasks and close AMQP and HTTP client resources."""
        tasks = list(self._background_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        for task in tasks:
            with suppress(BaseException):
                await task
        self._background_tasks.clear()

        await self._close_amqp()

        client = self._http_client
        http_loop = self._http_loop
        self._http_client = None
        self._http_loop = None
        if client is not None and not client.is_closed:
            with suppress(RuntimeError):
                if http_loop is asyncio.get_running_loop():
                    with suppress(Exception):
                        await client.aclose()

        self._telecom_online = False
        self._circuit_open_until = 0.0

    def get_status(self) -> dict[str, Any]:
        """Return current telecom gateway and circuit breaker status."""
        enabled = _telecom_enabled()
        return {
            "enabled": enabled != "false",
            "mode": enabled,
            "online": self._telecom_online,
            "bound": "BOUND_TRX" if self._telecom_online else "STANDBY",
            "gateway_url": _telecom_gateway_url(),
            "circuit_open": time.monotonic() < self._circuit_open_until,
        }


sms_dispatcher = SmsDispatcher()
