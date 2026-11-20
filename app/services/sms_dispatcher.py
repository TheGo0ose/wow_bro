from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from typing import Any

import httpx
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import get_settings
from app.services.billing_service import (
    CHANNEL_RATES_SOM,
    InsufficientBalanceError,
    count_sms_segments,
    create_cdr,
    precheck_balance,
)
from app.services.hlr_service import lookup_hlr_async

logger = logging.getLogger("otp_shield.telecom")

_DEMO_BULK_MSISDNS = (
    "996770730000",
    "996550730001",
    "996700730002",
    "996770730003",
    "996550730004",
    "996700730005",
    "996700730006",
    "996770730007",
    "996770730008",
    "996550730009",
)


def _telecom_enabled() -> str:
    return (os.environ.get("TELECOM_ENABLED") or get_settings().TELECOM_ENABLED).strip().lower()


def _telecom_gateway_url() -> str:
    return (os.environ.get("TELECOM_GATEWAY_URL") or get_settings().TELECOM_GATEWAY_URL).rstrip("/")


class SmsDispatcher:
    """Async telecom SMS gateway dispatcher with non-blocking circuit breaker."""

    def __init__(self) -> None:
        self._telecom_online: bool = False
        self._circuit_open_until: float = 0.0
        self._background_tasks: set[asyncio.Task[Any]] = set()

    async def probe_gateway(self) -> bool:
        """Probe telecom gateway /health without raising on connectivity failures."""
        if _telecom_enabled() == "false":
            self._telecom_online = False
            return False

        url = f"{_telecom_gateway_url()}/health"
        try:
            async with httpx.AsyncClient(timeout=0.4) as client:
                resp = await client.get(url)
            if resp.status_code == 200:
                self._telecom_online = True
                self._circuit_open_until = 0.0
                return True
        except (httpx.HTTPError, OSError) as exc:
            logger.debug("Telecom gateway probe failed: %s", type(exc).__name__)

        self._telecom_online = False
        return False

    def enqueue_otp_sms(self, redis_client: Redis, *, msisdn: str, code: str) -> None:
        """Schedule OTP dispatch in background if telecom gateway is online."""
        if _telecom_enabled() == "false" or not self._telecom_online:
            return
        if time.monotonic() < self._circuit_open_until:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        task = loop.create_task(
            self._send_otp_background(redis_client, msisdn=msisdn, code=code)
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _send_otp_background(
        self,
        redis_client: Redis,
        *,
        msisdn: str,
        code: str,
    ) -> None:
        msg_id = f"otp-{uuid.uuid4().hex[:12]}"
        text = f"Your OTP code: {code}"
        encoding, segments = count_sms_segments(text)
        url = f"{_telecom_gateway_url()}/send"
        payload = {
            "msg_id": msg_id,
            "msisdn": msisdn,
            "text": text,
            "channel": "otp_svc",
            "priority": 1,
            "validity_period": 2,
        }

        try:
            hlr = await lookup_hlr_async(redis_client, msisdn)
            await create_cdr(
                redis_client,
                msg_id=msg_id,
                msisdn=msisdn,
                channel="otp_svc",
                text_len=len(text),
                encoding=encoding,
                segments=segments,
                status="SUBMITTED",
                operator=hlr.operator,
                hlr_status=hlr.hlr_status,
            )
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code >= 400:
                self._mark_offline()
        except InsufficientBalanceError as exc:
            logger.warning("Skipping OTP telecom dispatch due to balance limit: %s", exc)
        except (httpx.HTTPError, RedisError, OSError):
            self._mark_offline()

    def _mark_offline(self) -> None:
        self._telecom_online = False
        self._circuit_open_until = time.monotonic() + 10.0

    async def send_bulk_batch(
        self,
        redis_client: Redis,
        *,
        count: int = 10,
        text: str = "Special promo offer!",
    ) -> dict[str, Any]:
        """Dispatch a promotional bulk SMS batch through the low-priority channel."""
        encoding, segments = count_sms_segments(text)
        total_cost = round(CHANNEL_RATES_SOM["bulk_mkt"] * segments * count, 2)
        await precheck_balance(redis_client, channel="bulk_mkt", required_som=total_cost)

        if (
            _telecom_enabled() != "false"
            and not self._telecom_online
            and time.monotonic() >= self._circuit_open_until
        ):
            await self.probe_gateway()

        messages: list[dict[str, Any]] = []
        msg_ids: list[str] = []

        for idx in range(count):
            msg_id = f"bulk-{uuid.uuid4().hex[:12]}"
            msisdn = (
                _DEMO_BULK_MSISDNS[idx]
                if idx < len(_DEMO_BULK_MSISDNS)
                else f"99677073{idx % 50:04d}"
            )
            msg_ids.append(msg_id)
            hlr = await lookup_hlr_async(redis_client, msisdn)
            await create_cdr(
                redis_client,
                msg_id=msg_id,
                msisdn=msisdn,
                channel="bulk_mkt",
                text_len=len(text),
                encoding=encoding,
                segments=segments,
                status="SUBMITTED",
                operator=hlr.operator,
                hlr_status=hlr.hlr_status,
            )
            messages.append(
                {
                    "msg_id": msg_id,
                    "msisdn": msisdn,
                    "text": text,
                    "channel": "bulk_mkt",
                    "priority": 0,
                    "validity_period": 60,
                }
            )

        dispatched = False
        if _telecom_enabled() != "false" and self._telecom_online:
            url = f"{_telecom_gateway_url()}/bulk"
            try:
                async with httpx.AsyncClient(timeout=3.0) as client:
                    resp = await client.post(url, json={"messages": messages})
                if resp.status_code < 400:
                    dispatched = True
                else:
                    self._mark_offline()
            except (httpx.HTTPError, OSError):
                self._mark_offline()

        return {
            "status": "queued" if dispatched else "simulated",
            "count": count,
            "channel": "bulk_mkt",
            "encoding": encoding,
            "segments": segments,
            "msg_ids": msg_ids,
            "telecom_online": self._telecom_online,
        }

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
