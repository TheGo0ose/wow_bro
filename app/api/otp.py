from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from redis.asyncio import Redis

from app.core.crypto import CODE_RE, MSISDN_RE
from app.core.redis_client import get_redis
from app.services.otp_service import otp_service

router = APIRouter(tags=["otp"])


class SendOtpRequest(BaseModel):
    """Payload for requesting a new OTP code."""

    msisdn: str

    @field_validator("msisdn")
    @classmethod
    def validate_msisdn(cls, value: str) -> str:
        if not MSISDN_RE.fullmatch(value):
            raise ValueError("msisdn must consist of 7 to 15 digits")
        return value


class VerifyOtpRequest(BaseModel):
    """Payload for verifying an OTP code."""

    msisdn: str
    code: str

    @field_validator("msisdn")
    @classmethod
    def validate_msisdn(cls, value: str) -> str:
        if not MSISDN_RE.fullmatch(value):
            raise ValueError("msisdn must consist of 7 to 15 digits")
        return value

    @field_validator("code")
    @classmethod
    def validate_code(cls, value: str) -> str:
        if not CODE_RE.fullmatch(value):
            raise ValueError("code must consist of strictly 6 digits")
        return value


def extract_client_ip(request: Request) -> str:
    """Resolve client IP from forwarding headers or peer address."""
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        first_ip = forwarded_for.split(",", 1)[0].strip()
        if first_ip:
            return first_ip

    for header in ("X-Real-IP", "X-Client-IP"):
        header_val = request.headers.get(header)
        if header_val:
            stripped = header_val.strip()
            if stripped:
                return stripped

    if request.client and request.client.host:
        return request.client.host

    return "127.0.0.1"


@router.post("/otp/send")
async def send_otp(
    payload: SendOtpRequest,
    request: Request,
    redis_client: Annotated[Redis, Depends(get_redis)],
) -> JSONResponse:
    """Generate and dispatch an OTP code for the given MSISDN."""
    client_ip = extract_client_ip(request)
    status_code, body, headers = await otp_service.send_otp(
        redis_client,
        msisdn=payload.msisdn,
        client_ip=client_ip,
    )
    return JSONResponse(status_code=status_code, content=body, headers=headers)


@router.post("/otp/verify")
async def verify_otp(
    payload: VerifyOtpRequest,
    request: Request,
    redis_client: Annotated[Redis, Depends(get_redis)],
) -> JSONResponse:
    """Verify an OTP code for the given MSISDN and client IP."""
    client_ip = extract_client_ip(request)
    status_code, body, headers = await otp_service.verify_otp(
        redis_client,
        msisdn=payload.msisdn,
        code=payload.code,
        client_ip=client_ip,
    )
    return JSONResponse(status_code=status_code, content=body, headers=headers)
