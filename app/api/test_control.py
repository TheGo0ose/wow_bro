from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from redis.asyncio import Redis

from app.core.clock import clock
from app.core.redis_client import get_redis

router = APIRouter(tags=["test-control"])


class AdvanceRequest(BaseModel):
    """Payload to advance the test clock by a number of seconds."""

    seconds: float


class FreezeRequest(BaseModel):
    """Payload to freeze the test clock at an optional epoch millisecond."""

    epoch_ms: int | None = None


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
    flush: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Reset the application clock and optionally flush Redis DB."""
    clock.reset()
    if flush:
        await redis_client.flushdb()
    return {
        "status": "ok",
        "flushed": flush,
        "now_ms": clock.now_ms(),
        "now_iso": clock.now_iso(),
    }
