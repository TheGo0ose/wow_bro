from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from redis.asyncio import Redis

from app.config import (
    clear_runtime_overrides,
    resolve_outbox_path,
    set_runtime_overrides,
)
from app.core.clock import clock
from app.core.redis_client import get_redis
from app.services.billing_service import reset_billing_state
from app.services.hlr_service import reset_hlr_overrides

router = APIRouter(tags=["test-control"])


class AdvanceRequest(BaseModel):
    """Payload to advance the test clock by a number of seconds."""

    seconds: float


class FreezeRequest(BaseModel):
    """Payload to freeze the test clock at an optional epoch millisecond."""

    epoch_ms: int | None = None


class ResetRequest(BaseModel):
    """Payload to reset test clock, Redis state, outbox, and runtime overrides."""

    flush: bool = False
    clear_outbox: bool | None = None
    start_time: str | None = None
    overrides: dict[str, Any] | None = None


@router.post("/_test/advance")
async def advance_clock(payload: AdvanceRequest) -> dict[str, Any]:
    """Advance the deterministic application clock."""
    now_ms = clock.advance(payload.seconds)
    return {"status": "ok", "now_ms": now_ms, "now_iso": clock.now_iso()}


@router.post("/_test/freeze")
async def freeze_clock(payload: FreezeRequest | None = None) -> dict[str, Any]:
    """Freeze the deterministic application clock."""
    epoch_ms = payload.epoch_ms if payload is not None else None
    now_ms = clock.freeze(epoch_ms)
    return {"status": "ok", "now_ms": now_ms, "now_iso": clock.now_iso()}


@router.post("/_test/reset")
async def reset_clock_and_state(
    redis_client: Annotated[Redis, Depends(get_redis)],
    payload: ResetRequest | None = None,
    flush: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Reset the clock, runtime overrides, and optionally flush Redis and outbox."""
    do_flush = flush or (payload.flush if payload is not None else False)
    do_clear_outbox = (
        payload.clear_outbox
        if (payload is not None and payload.clear_outbox is not None)
        else do_flush
    )

    if payload is not None and payload.overrides:
        applied = set_runtime_overrides(payload.overrides)
    else:
        clear_runtime_overrides()
        applied = {}

    if payload is not None and payload.start_time:
        raw_start = payload.start_time.strip()
        if raw_start.endswith("Z"):
            raw_start = f"{raw_start[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(raw_start)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid start_time ISO format") from exc
        epoch_ms = round(dt.timestamp() * 1000)
        clock.freeze(epoch_ms)
    else:
        clock.reset()

    if do_flush:
        await redis_client.flushdb()
        await reset_billing_state(redis_client)
        await reset_hlr_overrides(redis_client)

    if do_clear_outbox:
        outbox_path = resolve_outbox_path()
        outbox_path.parent.mkdir(parents=True, exist_ok=True)
        outbox_path.write_text("", encoding="utf-8")

    return {
        "status": "ok",
        "flushed": do_flush,
        "outbox_cleared": do_clear_outbox,
        "overrides": applied,
        "now_ms": clock.now_ms(),
        "now_iso": clock.now_iso(),
    }
