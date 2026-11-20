from __future__ import annotations

import math
from typing import Any

from redis.asyncio import Redis

from app.core.clock import clock

BALANCES_KEY = "billing:balances"
CDR_IDS_KEY = "billing:cdr_ids"
CDR_KEY_PREFIX = "billing:cdr:"

DEFAULT_BALANCES: dict[str, str] = {
    "otp_svc": "5000.00",
    "bulk_mkt": "1000.00",
}

CHANNEL_RATES_SOM: dict[str, float] = {
    "otp_svc": 0.45,
    "bulk_mkt": 0.30,
}

_GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ ÆæßÉ !\"#¤%&'()*+,-./"
    "0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿"
    "abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXT = frozenset("^{}\\[~]|€\f")


def count_sms_segments(text: str) -> tuple[str, int]:
    """Determine SMS encoding (GSM-7 or UCS-2) and segment count."""
    if all(ch in _GSM7_BASIC or ch in _GSM7_EXT for ch in text):
        septets = sum(2 if ch in _GSM7_EXT else 1 for ch in text)
        if septets <= 160:
            return ("GSM-7", 1)
        return ("GSM-7", math.ceil(septets / 153))

    utf16_units = len(text.encode("utf-16-be")) // 2
    if utf16_units <= 70:
        return ("UCS-2", 1)
    return ("UCS-2", math.ceil(utf16_units / 67))


async def ensure_balances(redis_client: Redis) -> dict[str, float]:
    """Initialize default telecom account balances if absent and return them."""
    for channel, initial in DEFAULT_BALANCES.items():
        await redis_client.hsetnx(BALANCES_KEY, channel, initial)

    raw = await redis_client.hgetall(BALANCES_KEY)
    balances: dict[str, float] = {}
    for channel, amount in raw.items():
        try:
            balances[channel] = round(float(amount), 2)
        except (TypeError, ValueError):
            balances[channel] = 0.0
    return balances


async def create_cdr(
    redis_client: Redis,
    *,
    msg_id: str,
    msisdn: str,
    channel: str,
    text_len: int,
    encoding: str,
    segments: int,
    status: str = "SUBMITTED",
) -> dict[str, Any]:
    """Persist a Call Detail Record and atomically deduct channel balance."""
    await ensure_balances(redis_client)

    rate = CHANNEL_RATES_SOM.get(channel, 0.45)
    cost_som = round(rate * segments, 2)
    ts = clock.now_iso()
    cdr_key = f"{CDR_KEY_PREFIX}{msg_id}"

    cdr_fields = {
        "msg_id": msg_id,
        "msisdn": msisdn,
        "channel": channel,
        "text_len": str(text_len),
        "encoding": encoding,
        "segments": str(segments),
        "cost_som": f"{cost_som:.2f}",
        "status": status,
        "ts": ts,
    }

    async with redis_client.pipeline(transaction=False) as pipe:
        pipe.hincrbyfloat(BALANCES_KEY, channel, -cost_som)
        pipe.hset(cdr_key, mapping=cdr_fields)
        pipe.expire(cdr_key, 86400)
        pipe.lpush(CDR_IDS_KEY, msg_id)
        pipe.ltrim(CDR_IDS_KEY, 0, 99)
        await pipe.execute()

    return {
        "msg_id": msg_id,
        "msisdn": msisdn,
        "channel": channel,
        "text_len": text_len,
        "encoding": encoding,
        "segments": segments,
        "cost_som": cost_som,
        "status": status,
        "ts": ts,
    }


async def update_cdr_status(
    redis_client: Redis,
    *,
    msg_id: str,
    status: str,
    msisdn: str = "",
) -> bool:
    """Update delivery status of an existing CDR."""
    cdr_key = f"{CDR_KEY_PREFIX}{msg_id}"
    exists = await redis_client.exists(cdr_key)
    if not exists:
        return False

    updates: dict[str, str] = {
        "status": status,
        "updated_at": clock.now_iso(),
    }
    if msisdn:
        current_msisdn = await redis_client.hget(cdr_key, "msisdn")
        if not current_msisdn:
            updates["msisdn"] = msisdn

    await redis_client.hset(cdr_key, mapping=updates)
    return True


async def get_billing_snapshot(
    redis_client: Redis,
    limit: int = 50,
) -> dict[str, Any]:
    """Return current account balances and recent CDR records."""
    balances = await ensure_balances(redis_client)
    if limit <= 0:
        return {"balances": balances, "cdrs": []}

    cdr_ids = await redis_client.lrange(CDR_IDS_KEY, 0, limit - 1)
    if not cdr_ids:
        return {"balances": balances, "cdrs": []}

    async with redis_client.pipeline(transaction=False) as pipe:
        for msg_id in cdr_ids:
            pipe.hgetall(f"{CDR_KEY_PREFIX}{msg_id}")
        raw_cdrs = await pipe.execute()

    cdrs: list[dict[str, Any]] = []
    for row in raw_cdrs:
        if not row:
            continue
        cdrs.append(
            {
                "msg_id": row.get("msg_id", ""),
                "msisdn": row.get("msisdn", ""),
                "channel": row.get("channel", ""),
                "text_len": int(row.get("text_len", 0) or 0),
                "encoding": row.get("encoding", "GSM-7"),
                "segments": int(row.get("segments", 1) or 1),
                "cost_som": round(float(row.get("cost_som", 0.0) or 0.0), 2),
                "status": row.get("status", "SUBMITTED"),
                "ts": row.get("ts", ""),
                "updated_at": row.get("updated_at", ""),
            }
        )

    return {"balances": balances, "cdrs": cdrs}
