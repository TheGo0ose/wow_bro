from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from redis.asyncio import Redis

from app.core.clock import clock

STREAM_KEY = "security:events"
STATS_KEY = "security:stats"

audit_logger = logging.getLogger("otp_shield.audit")

_subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

_EVENT_COUNTER_MAP: dict[str, str] = {
    "OTP_SENT": "otp_sent",
    "OTP_VERIFIED": "otp_verified",
    "OTP_FAILED": "otp_failed",
    "OTP_INVALID_OR_USED": "otp_failed",
    "OTP_EXPIRED": "otp_expired",
    "MSISDN_LOCKED": "msisdn_locked",
    "RATE_LIMITED_SEND": "rate_limited_send",
    "RATE_LIMITED_SEND_10M": "rate_limited_send",
    "RATE_LIMITED_COOLDOWN": "rate_limited_send",
    "RATE_LIMITED_IP": "rate_limited_ip",
}

_DEFAULT_COUNTERS: dict[str, int] = {
    "total_events": 0,
    "otp_sent": 0,
    "otp_verified": 0,
    "otp_failed": 0,
    "otp_expired": 0,
    "msisdn_locked": 0,
    "rate_limited_send": 0,
    "rate_limited_ip": 0,
}


def broadcast(payload: dict[str, Any]) -> None:
    """Push a security event payload to all active SSE subscriber queues."""
    for queue in list(_subscribers):
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            continue


async def subscribe_events() -> AsyncIterator[dict[str, Any]]:
    """Yield live security events for SSE clients."""
    queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
    _subscribers.add(queue)
    try:
        while True:
            event = await queue.get()
            yield event
    finally:
        _subscribers.discard(queue)


async def log_security_event(
    redis_client: Redis,
    *,
    event: str,
    msisdn: str = "",
    client_ip: str = "",
    status_code: int = 200,
    detail: str = "",
) -> dict[str, Any]:
    """Record a security event in Redis Stream, stats hash, audit logger, and SSE."""
    payload: dict[str, Any] = {
        "ts": clock.now_iso(),
        "event": event,
        "msisdn": msisdn,
        "client_ip": client_ip,
        "status_code": str(status_code),
        "detail": detail,
    }

    async with redis_client.pipeline(transaction=False) as pipe:
        pipe.xadd(STREAM_KEY, payload, maxlen=1000, approximate=True)
        pipe.hincrby(STATS_KEY, "total_events", 1)
        counter_field = _EVENT_COUNTER_MAP.get(event)
        if counter_field:
            pipe.hincrby(STATS_KEY, counter_field, 1)
        await pipe.execute()

    audit_logger.info(json.dumps(payload, ensure_ascii=False))
    broadcast(payload)
    return payload


async def get_security_stats(
    redis_client: Redis,
    *,
    msisdn: str | None = None,
    client_ip: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Return aggregated security counters, active lock count, and recent events."""
    raw_stats = await redis_client.hgetall(STATS_KEY)
    counters = dict(_DEFAULT_COUNTERS)
    for key, value in raw_stats.items():
        try:
            counters[key] = int(value)
        except (TypeError, ValueError):
            counters[key] = 0

    stream_entries = await redis_client.xrevrange(STREAM_KEY, max="+", min="-", count=200)
    events: list[dict[str, Any]] = []
    for entry_id, fields in stream_entries:
        if msisdn and fields.get("msisdn") != msisdn:
            continue
        if client_ip and fields.get("client_ip") != client_ip:
            continue
        events.append({"id": entry_id, **fields})
        if len(events) >= limit:
            break

    now_ms = clock.now_ms()
    active_locks = 0
    cursor = 0
    while True:
        cursor, keys = await redis_client.scan(cursor=cursor, match="lock:*", count=100)
        if keys:
            values = await redis_client.mget(keys)
            for val in values:
                if val is None:
                    continue
                try:
                    if now_ms < int(val):
                        active_locks += 1
                except (TypeError, ValueError):
                    continue
        if cursor == 0:
            break

    return {
        "counters": counters,
        "active_locks": active_locks,
        "successful_breaches": 0,
        "events": events,
    }
