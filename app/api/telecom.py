from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.services.audit import broadcast_telecom_dlr
from app.services.sms_dispatcher import sms_dispatcher

router = APIRouter(tags=["telecom"])


class DlrCallback(BaseModel):
    """Delivery receipt callback payload from telecom gateway."""

    msg_id: str
    msisdn: str = ""
    status: Literal["DELIVRD", "EXPIRED", "UNDELIV", "SUBMITTED"]
    channel: str = ""
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
async def telecom_dlr(payload: DlrCallback) -> dict[str, Any]:
    """Accept a telecom DLR webhook and notify SSE clients without polluting audit logs."""
    broadcast_telecom_dlr(payload.model_dump())
    return {
        "status": "ok",
        "updated": True,
        "msg_id": payload.msg_id,
        "dlr_status": payload.status,
    }


@router.get("/telecom/status")
async def telecom_status() -> dict[str, Any]:
    """Return telecom gateway health and current operator billing/HLR snapshot."""
    return await sms_dispatcher.get_telecom_snapshot()


@router.post("/telecom/bulk", response_model=None)
async def telecom_bulk(payload: BulkRequest) -> JSONResponse:
    """Proxy a throttled bulk SMS batch to the telecom operator gateway."""
    code, body = await sms_dispatcher.proxy_bulk(payload.count, payload.text)
    return JSONResponse(status_code=code, content=body)


@router.post("/telecom/topup", response_model=None)
async def telecom_topup(payload: TopupRequest) -> JSONResponse:
    """Proxy a channel account balance top-up to the telecom operator gateway."""
    code, body = await sms_dispatcher.proxy_topup(payload.channel, payload.amount)
    return JSONResponse(status_code=code, content=body)


@router.get("/telecom/hlr", response_model=None)
async def telecom_hlr_lookup(msisdn: str) -> JSONResponse:
    """Proxy an HLR/MNP routing lookup to the telecom operator gateway."""
    code, body = await sms_dispatcher.proxy_hlr_lookup(msisdn)
    return JSONResponse(status_code=code, content=body)


@router.post("/telecom/hlr/mnp", response_model=None)
async def telecom_hlr_mnp(payload: HlrMnpRequest) -> JSONResponse:
    """Proxy an MNP operator override or network presence update to the operator gateway."""
    code, body = await sms_dispatcher.proxy_hlr_mnp(
        payload.msisdn,
        payload.operator,
        payload.in_network,
    )
    return JSONResponse(status_code=code, content=body)
