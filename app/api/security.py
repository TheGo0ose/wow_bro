from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import suppress
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis

from app.core.redis_client import get_redis
from app.services.audit import get_security_stats, subscribe_events
from app.services.billing_service import get_billing_snapshot
from app.services.sms_dispatcher import sms_dispatcher

router = APIRouter(tags=["security"])


@router.get("/security/stats")
async def security_stats(
    redis_client: Annotated[Redis, Depends(get_redis)],
    msisdn: Annotated[str | None, Query()] = None,
    ip: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> dict[str, Any]:
    """Return aggregated security audit counters, events, and telecom state."""
    stats = await get_security_stats(
        redis_client,
        msisdn=msisdn,
        client_ip=ip,
        limit=limit,
    )
    billing = await get_billing_snapshot(redis_client)
    return {
        "counters": stats.get("counters", {}),
        "active_locks": stats.get("active_locks", 0),
        "successful_breaches": stats.get("successful_breaches", 0),
        "events": stats.get("events", []),
        "telecom": {
            "gateway": sms_dispatcher.get_status(),
            "balances": billing.get("balances", {}),
            "cdrs": billing.get("cdrs", []),
        },
    }


@router.get("/security/stream")
async def security_stream(request: Request) -> StreamingResponse:
    """Stream real-time security events over Server-Sent Events (SSE)."""

    async def event_generator() -> AsyncIterator[str]:
        stream = subscribe_events()
        pending: asyncio.Task[dict[str, Any]] | None = None
        try:
            while True:
                if await request.is_disconnected():
                    break
                if pending is None:
                    pending = asyncio.create_task(anext(stream))
                # Use asyncio.wait instead of wait_for so a 15s keepalive timeout
                # does not cancel the underlying subscribe_events() generator.
                done, _ = await asyncio.wait({pending}, timeout=15.0)
                if not done:
                    yield ": keepalive\n\n"
                    continue
                event = pending.result()
                pending = None
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except (StopAsyncIteration, asyncio.CancelledError):
            pass
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with suppress(BaseException):
                    await pending
            with suppress(BaseException):
                await stream.aclose()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
