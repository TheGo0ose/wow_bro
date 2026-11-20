from __future__ import annotations

import os
import uuid
from typing import Any

from redis.asyncio import Redis

from app.config import get_settings
from app.core.clock import clock
from app.core.crypto import generate_otp_code, hash_otp
from app.core.lua_scripts import execute_send_otp, execute_verify_otp
from app.services.audit import log_security_event
from app.services.outbox import write_outbox_sms
from app.services.sms_dispatcher import sms_dispatcher


def _get_hmac_secret() -> str:
    return os.environ.get("HMAC_SECRET") or get_settings().HMAC_SECRET


class OtpService:
    """Core OTP issuance and verification orchestration service."""

    async def send_otp(
        self,
        redis_client: Redis,
        *,
        msisdn: str,
        client_ip: str = "127.0.0.1",
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        """Issue a 6-digit OTP, write outbox delivery, and enforce cooldown/rate limits."""
        settings = get_settings()
        secret = _get_hmac_secret()
        code = generate_otp_code()
        otp_hash = hash_otp(secret, msisdn, code)
        now_ms = clock.now_ms()
        req_id = uuid.uuid4().hex[:12]

        res = await execute_send_otp(
            redis_client,
            msisdn=msisdn,
            now_ms=now_ms,
            otp_hash=otp_hash,
            settings=settings,
            req_id=req_id,
        )

        if res.status_code == 200:
            write_outbox_sms(msisdn, code, ts=clock.now_iso())
            await log_security_event(
                redis_client,
                event=res.event,
                msisdn=msisdn,
                client_ip=client_ip,
                status_code=200,
                detail="OTP issued",
            )
            sms_dispatcher.enqueue_otp_sms(redis_client, msisdn=msisdn, code=code)
            return (
                200,
                {"status": "sent", "msisdn": msisdn, "expires_in": settings.OTP_TTL},
                {},
            )

        if res.status_code in (423, 429):
            await log_security_event(
                redis_client,
                event=res.event,
                msisdn=msisdn,
                client_ip=client_ip,
                status_code=res.status_code,
                detail=f"retry_after={res.retry_after}",
            )
            return (
                res.status_code,
                {"detail": res.event, "retry_after": res.retry_after},
                {"Retry-After": str(res.retry_after)},
            )

        await log_security_event(
            redis_client,
            event=res.event,
            msisdn=msisdn,
            client_ip=client_ip,
            status_code=res.status_code,
            detail="",
        )
        return (res.status_code, {"detail": res.event}, {})

    async def verify_otp(
        self,
        redis_client: Redis,
        *,
        msisdn: str,
        code: str,
        client_ip: str,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        """Verify an OTP code atomically and log security audit events without the code."""
        settings = get_settings()
        secret = _get_hmac_secret()
        candidate_hash = hash_otp(secret, msisdn, code)
        now_ms = clock.now_ms()
        req_id = uuid.uuid4().hex[:12]

        res = await execute_verify_otp(
            redis_client,
            msisdn=msisdn,
            client_ip=client_ip,
            now_ms=now_ms,
            candidate_hash=candidate_hash,
            settings=settings,
            req_id=req_id,
        )

        if res.status_code == 200:
            await log_security_event(
                redis_client,
                event=res.event,
                msisdn=msisdn,
                client_ip=client_ip,
                status_code=200,
                detail="OTP verified",
            )
            return (200, {"status": "verified", "msisdn": msisdn}, {})

        if res.status_code in (423, 429):
            await log_security_event(
                redis_client,
                event=res.event,
                msisdn=msisdn,
                client_ip=client_ip,
                status_code=res.status_code,
                detail=f"retry_after={res.value}",
            )
            return (
                res.status_code,
                {"detail": res.event, "retry_after": res.value},
                {"Retry-After": str(res.value)},
            )

        if res.status_code == 410:
            await log_security_event(
                redis_client,
                event=res.event,
                msisdn=msisdn,
                client_ip=client_ip,
                status_code=410,
                detail="OTP expired",
            )
            return (410, {"detail": res.event}, {})

        await log_security_event(
            redis_client,
            event=res.event,
            msisdn=msisdn,
            client_ip=client_ip,
            status_code=401,
            detail=f"attempts_remaining={res.value}",
        )
        return (
            401,
            {"detail": res.event, "attempts_remaining": res.value},
            {},
        )


otp_service = OtpService()
