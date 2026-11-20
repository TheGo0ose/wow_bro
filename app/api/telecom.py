from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.core.redis_client import get_redis
from app.services.billing_service import get_billing_snapshot, update_cdr_status
from app.services.sms_dispatcher import sms_dispatcher

router = APIRouter(tags=["telecom"])


class DlrCallback(BaseModel):
    """Delivery receipt callback payload from telecom gateway."""

    msg_id: str
    msisdn: str = ""
    status: Literal["DELIVRD", "EXPIRED", "UNDELIV", "SUBMITTED"]


class BulkRequest(BaseModel):
    """Request payload to trigger a bulk marketing SMS batch."""

    count: int = Field(default=10, ge=1, le=200)
    text: str = Field(default="Akciya! Skidki 20% tolko segodnya!")


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


@router.post("/telecom/bulk")
async def telecom_bulk(
    payload: BulkRequest,
    redis_client: Annotated[Redis, Depends(get_redis)],
) -> dict[str, Any]:
    """Dispatch a throttled bulk SMS batch via the bulk_mkt channel."""
    return await sms_dispatcher.send_bulk_batch(
        redis_client,
        count=payload.count,
        text=payload.text,
    )
