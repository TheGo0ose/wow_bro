from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.core.redis_client import get_redis
from app.services.billing_service import (
    InsufficientBalanceError,
    get_billing_snapshot,
    topup_balance,
    update_cdr_status,
)
from app.services.hlr_service import lookup_hlr_async, register_mnp
from app.services.sms_dispatcher import sms_dispatcher

router = APIRouter(tags=["telecom"])


class DlrCallback(BaseModel):
    """Delivery receipt callback payload from telecom gateway."""

    msg_id: str
    msisdn: str = ""
    status: Literal["DELIVRD", "EXPIRED", "UNDELIV", "SUBMITTED"]
    operator: str = ""
    hlr_status: str = ""


class BulkRequest(BaseModel):
    """Request payload to trigger a bulk marketing SMS batch."""

    count: int = Field(default=10, ge=1, le=200)
    text: str = Field(default="Akciya! Skidki 20% tolko segodnya!")


class TopupRequest(BaseModel):
    """Request payload to top up a telecom channel balance."""

    channel: Literal["otp_svc", "bulk_mkt"] = "bulk_mkt"
    amount: float = Field(gt=0, le=100000)


class HlrMnpRequest(BaseModel):
    """Request payload to register MNP porting or subscriber presence."""

    msisdn: str
    operator: str | None = None
    in_network: bool | None = None


@router.post("/telecom/dlr")
async def telecom_dlr(
    payload: DlrCallback,
    redis_client: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    """Update a CDR delivery status from a telecom DLR callback."""
    updated = await update_cdr_status(
        redis_client,
        msg_id=payload.msg_id,
        status=payload.status,
        msisdn=payload.msisdn,
        operator=payload.operator,
        hlr_status=payload.hlr_status,
    )
    return {
        "status": "ok",
        "updated": updated,
        "msg_id": payload.msg_id,
        "dlr_status": payload.status,
    }


@router.get("/telecom/status")
async def telecom_status(
    redis_client: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    """Return telecom gateway health and current billing snapshot."""
    snapshot = await get_billing_snapshot(redis_client)
    return {
        "gateway": sms_dispatcher.get_status(),
        **snapshot,
    }


@router.post("/telecom/bulk", response_model=None)
async def telecom_bulk(
    payload: BulkRequest,
    redis_client: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any] | JSONResponse:
    """Dispatch a throttled bulk SMS batch via the bulk_mkt channel."""
    try:
        return await sms_dispatcher.send_bulk_batch(
            redis_client,
            count=payload.count,
            text=payload.text,
        )
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


@router.post("/telecom/topup")
async def telecom_topup(
    payload: TopupRequest,
    redis_client: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    """Top up a telecom channel account balance."""
    balances = await topup_balance(
        redis_client,
        channel=payload.channel,
        amount_som=payload.amount,
    )
    return {
        "status": "ok",
        "channel": payload.channel,
        "amount": round(float(payload.amount), 2),
        "balances": balances,
    }


@router.get("/telecom/hlr")
async def telecom_hlr_lookup(
    msisdn: str,
    redis_client: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    """Perform an HLR/MNP routing lookup for an MSISDN."""
    result = await lookup_hlr_async(redis_client, msisdn)
    return result.to_dict()


@router.post("/telecom/hlr/mnp")
async def telecom_hlr_mnp(
    payload: HlrMnpRequest,
    redis_client: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    """Update MNP operator override or network presence for an MSISDN."""
    try:
        result = await register_mnp(
            redis_client,
            msisdn=payload.msisdn,
            operator=payload.operator,
            in_network=payload.in_network,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return result.to_dict()
