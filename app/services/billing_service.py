from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from contextlib import suppress
from decimal import Decimal
from typing import Any

import asyncpg
from redis.asyncio import Redis

from app.config import get_settings
from app.core.clock import clock
from app.services.hlr_service import get_hlr_summary_async

logger = logging.getLogger("otp_shield.billing")

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

_DEDUCT_BALANCE_LUA = """
local bal_raw = redis.call('HGET', KEYS[1], ARGV[1])
local bal = tonumber(bal_raw or '0') or 0
local bal_cents = math.floor(bal * 100 + 0.5)
local cost_cents = math.floor((tonumber(ARGV[2]) or 0) * 100 + 0.5)
if ARGV[3] == '1' and bal_cents < cost_cents then
    return {0, string.format('%.2f', bal_cents / 100)}
end
local new_cents = bal_cents - cost_cents
redis.call('HSET', KEYS[1], ARGV[1], string.format('%.2f', new_cents / 100))
return {1, string.format('%.2f', new_cents / 100)}
"""

_TOPUP_BALANCE_LUA = """
local bal_raw = redis.call('HGET', KEYS[1], ARGV[1])
local bal = tonumber(bal_raw or '0') or 0
local bal_cents = math.floor(bal * 100 + 0.5)
local add_cents = math.floor((tonumber(ARGV[2]) or 0) * 100 + 0.5)
local new_cents = bal_cents + add_cents
redis.call('HSET', KEYS[1], ARGV[1], string.format('%.2f', new_cents / 100))
return string.format('%.2f', new_cents / 100)
"""

_PG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sdp_accounts (
    channel VARCHAR(32) PRIMARY KEY,
    balance NUMERIC(12, 2) NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sdp_cdrs (
    seq BIGSERIAL PRIMARY KEY,
    msg_id VARCHAR(64) UNIQUE NOT NULL,
    msisdn VARCHAR(32) NOT NULL,
    channel VARCHAR(32) NOT NULL,
    operator VARCHAR(64) NOT NULL,
    hlr_status VARCHAR(32) NOT NULL,
    text_len INT NOT NULL,
    encoding VARCHAR(16) NOT NULL,
    segments INT NOT NULL,
    cost_som NUMERIC(10, 2) NOT NULL,
    status VARCHAR(24) NOT NULL,
    ts TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_PG_RETRY_BACKOFF_SECONDS = 3.0
_PG_CONN_ERRORS = (
    asyncpg.InterfaceError,
    asyncpg.ConnectionDoesNotExistError,
    asyncpg.PostgresConnectionError,
    OSError,
    asyncio.TimeoutError,
)

_pg_pool: asyncpg.Pool | None = None
_pg_loop: asyncio.AbstractEventLoop | None = None
_pg_dsn: str = ""
_pg_retry_after: float = 0.0
_pg_lock: asyncio.Lock | None = None


class InsufficientBalanceError(Exception):
    """Raised when a telecom channel has insufficient balance for a message/batch."""

    def __init__(self, channel: str, balance: float, required: float) -> None:
        self.channel = channel
        self.balance = round(float(balance), 2)
        self.required = round(float(required), 2)
        super().__init__(
            f"Insufficient balance for {self.channel}: "
            f"{self.balance:.2f} < {self.required:.2f} som"
        )


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


def _resolve_dsn() -> str:
    if "DATABASE_URL" in os.environ:
        raw = os.environ["DATABASE_URL"]
    else:
        raw = getattr(get_settings(), "DATABASE_URL", "")
    dsn = (raw or "").strip()
    if not dsn or dsn.lower() in {"none", "redis", "off", "false", "disabled"}:
        return ""
    return dsn


def _discard_pg_pool(*, backoff: bool = False) -> None:
    global _pg_pool, _pg_loop, _pg_dsn, _pg_retry_after
    _pg_pool = None
    _pg_loop = None
    _pg_dsn = ""
    if backoff:
        _pg_retry_after = time.monotonic() + _PG_RETRY_BACKOFF_SECONDS


async def _seed_pg_defaults(conn: asyncpg.Connection) -> None:
    now_iso = clock.now_iso()
    for channel, initial in DEFAULT_BALANCES.items():
        await conn.execute(
            """
            INSERT INTO sdp_accounts (channel, balance, updated_at)
            VALUES ($1, $2, $3)
            ON CONFLICT (channel) DO NOTHING
            """,
            channel,
            Decimal(initial),
            now_iso,
        )


async def _get_pg_pool() -> asyncpg.Pool | None:
    global _pg_pool, _pg_loop, _pg_dsn, _pg_retry_after, _pg_lock

    dsn = _resolve_dsn()
    if not dsn:
        if _pg_pool is not None:
            await close_billing()
        return None

    current_loop = asyncio.get_running_loop()
    if _pg_loop is not None and _pg_loop is not current_loop:
        # Discard pool bound to a previous pytest event loop without awaiting across loops.
        _discard_pg_pool(backoff=False)
        _pg_lock = None

    if _pg_pool is not None and _pg_dsn != dsn:
        await close_billing()

    if _pg_pool is not None:
        return _pg_pool

    if time.monotonic() < _pg_retry_after:
        return None

    if _pg_lock is None:
        _pg_lock = asyncio.Lock()

    async with _pg_lock:
        if _pg_pool is not None and _pg_loop is current_loop and _pg_dsn == dsn:
            return _pg_pool
        if time.monotonic() < _pg_retry_after:
            return None

        pool: asyncpg.Pool | None = None
        try:
            pool = await asyncio.wait_for(
                asyncpg.create_pool(
                    dsn,
                    min_size=1,
                    max_size=5,
                    timeout=1.5,
                    command_timeout=5.0,
                ),
                timeout=2.0,
            )
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute(_PG_SCHEMA_SQL)
                await _seed_pg_defaults(conn)
        except (TimeoutError, asyncpg.PostgresError, OSError, ValueError) as exc:
            logger.debug(
                "PostgreSQL SDP-07 unavailable (%s); using Redis fallback",
                type(exc).__name__,
            )
            if pool is not None:
                with suppress(Exception):
                    await pool.close()
            _discard_pg_pool(backoff=True)
            return None

        _pg_pool = pool
        _pg_loop = current_loop
        _pg_dsn = dsn
        _pg_retry_after = 0.0
        return _pg_pool


async def close_billing() -> None:
    """Close the active PostgreSQL billing pool cleanly if bound to the current loop."""
    global _pg_pool, _pg_loop, _pg_dsn, _pg_retry_after, _pg_lock
    pool = _pg_pool
    pool_loop = _pg_loop
    _pg_pool = None
    _pg_loop = None
    _pg_dsn = ""
    _pg_retry_after = 0.0
    _pg_lock = None

    if pool is not None:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is not None and pool_loop is running_loop:
            with suppress(Exception):
                await pool.close()


async def _ensure_redis_balances(redis_client: Redis) -> dict[str, float]:
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


async def ensure_balances(redis_client: Redis) -> dict[str, float]:
    """Initialize default telecom account balances if absent and return them."""
    pool = await _get_pg_pool()
    if pool is not None:
        try:
            async with pool.acquire() as conn, conn.transaction():
                await _seed_pg_defaults(conn)
                rows = await conn.fetch("SELECT channel, balance FROM sdp_accounts")
            return {row["channel"]: round(float(row["balance"]), 2) for row in rows}
        except _PG_CONN_ERRORS as exc:
            logger.debug("PostgreSQL read failed (%s); falling back to Redis", type(exc).__name__)
            _discard_pg_pool(backoff=True)

    return await _ensure_redis_balances(redis_client)


async def precheck_balance(
    redis_client: Redis,
    *,
    channel: str,
    required_som: float,
) -> float:
    """Verify channel balance covers required_som or raise InsufficientBalanceError."""
    required = round(float(required_som), 2)
    balances = await ensure_balances(redis_client)
    balance = round(float(balances.get(channel, 0.0)), 2)
    if balance < required:
        raise InsufficientBalanceError(channel=channel, balance=balance, required=required)
    return balance


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
    operator: str = "",
    hlr_status: str = "HLR_OK",
    enforce_balance: bool = True,
) -> dict[str, Any]:
    """Persist a Call Detail Record and atomically deduct channel balance."""
    rate = CHANNEL_RATES_SOM.get(channel, 0.45)
    cost_som = round(rate * segments, 2)
    ts = clock.now_iso()
    updated_at = ts

    pool = await _get_pg_pool()
    if pool is not None:
        try:
            async with pool.acquire() as conn, conn.transaction():
                await _seed_pg_defaults(conn)
                if channel not in DEFAULT_BALANCES:
                    await conn.execute(
                        """
                            INSERT INTO sdp_accounts (channel, balance, updated_at)
                            VALUES ($1, $2, $3)
                            ON CONFLICT (channel) DO NOTHING
                            """,
                        channel,
                        Decimal("0.00"),
                        ts,
                    )
                row = await conn.fetchrow(
                    "SELECT balance FROM sdp_accounts WHERE channel = $1 FOR UPDATE",
                    channel,
                )
                current_bal = round(float(row["balance"]), 2) if row is not None else 0.0
                if enforce_balance and current_bal < cost_som:
                    raise InsufficientBalanceError(
                        channel=channel,
                        balance=current_bal,
                        required=cost_som,
                    )

                new_bal = round(current_bal - cost_som, 2)
                await conn.execute(
                    """
                        UPDATE sdp_accounts
                        SET balance = $1, updated_at = $2
                        WHERE channel = $3
                        """,
                    Decimal(f"{new_bal:.2f}"),
                    updated_at,
                    channel,
                )
                await conn.execute(
                    """
                        INSERT INTO sdp_cdrs (
                            msg_id, msisdn, channel, operator, hlr_status,
                            text_len, encoding, segments, cost_som, status, ts, updated_at
                        )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                        """,
                    msg_id,
                    msisdn,
                    channel,
                    operator,
                    hlr_status,
                    text_len,
                    encoding,
                    segments,
                    Decimal(f"{cost_som:.2f}"),
                    status,
                    ts,
                    updated_at,
                )
            return {
                "msg_id": msg_id,
                "msisdn": msisdn,
                "channel": channel,
                "operator": operator,
                "hlr_status": hlr_status,
                "text_len": text_len,
                "encoding": encoding,
                "segments": segments,
                "cost_som": cost_som,
                "status": status,
                "ts": ts,
                "updated_at": updated_at,
            }
        except _PG_CONN_ERRORS as exc:
            logger.debug(
                "PostgreSQL CDR write failed (%s); falling back to Redis",
                type(exc).__name__,
            )
            _discard_pg_pool(backoff=True)

    await _ensure_redis_balances(redis_client)
    deduct_script = redis_client.register_script(_DEDUCT_BALANCE_LUA)
    result = await deduct_script(
        keys=[BALANCES_KEY],
        args=[channel, f"{cost_som:.2f}", "1" if enforce_balance else "0"],
    )
    if int(result[0]) == 0:
        raise InsufficientBalanceError(
            channel=channel,
            balance=round(float(result[1]), 2),
            required=cost_som,
        )

    cdr_key = f"{CDR_KEY_PREFIX}{msg_id}"
    cdr_fields = {
        "msg_id": msg_id,
        "msisdn": msisdn,
        "channel": channel,
        "operator": operator,
        "hlr_status": hlr_status,
        "text_len": str(text_len),
        "encoding": encoding,
        "segments": str(segments),
        "cost_som": f"{cost_som:.2f}",
        "status": status,
        "ts": ts,
        "updated_at": updated_at,
    }

    async with redis_client.pipeline(transaction=False) as pipe:
        pipe.hset(cdr_key, mapping=cdr_fields)
        pipe.expire(cdr_key, 86400)
        pipe.lpush(CDR_IDS_KEY, msg_id)
        pipe.ltrim(CDR_IDS_KEY, 0, 99)
        await pipe.execute()

    return {
        "msg_id": msg_id,
        "msisdn": msisdn,
        "channel": channel,
        "operator": operator,
        "hlr_status": hlr_status,
        "text_len": text_len,
        "encoding": encoding,
        "segments": segments,
        "cost_som": cost_som,
        "status": status,
        "ts": ts,
        "updated_at": updated_at,
    }


async def update_cdr_status(
    redis_client: Redis,
    *,
    msg_id: str,
    status: str,
    msisdn: str = "",
    operator: str = "",
    hlr_status: str = "",
) -> bool:
    """Update delivery status (and optional HLR metadata) of an existing CDR."""
    now_iso = clock.now_iso()

    pool = await _get_pg_pool()
    if pool is not None:
        try:
            async with pool.acquire() as conn, conn.transaction():
                row = await conn.fetchrow(
                    "SELECT msisdn, operator, hlr_status FROM sdp_cdrs WHERE msg_id = $1",
                    msg_id,
                )
                if row is None:
                    return False
                next_msisdn = row["msisdn"] or msisdn
                next_operator = operator if operator else row["operator"]
                next_hlr_status = hlr_status if hlr_status else row["hlr_status"]
                await conn.execute(
                    """
                        UPDATE sdp_cdrs
                        SET status = $1,
                            msisdn = $2,
                            operator = $3,
                            hlr_status = $4,
                            updated_at = $5
                        WHERE msg_id = $6
                        """,
                    status,
                    next_msisdn,
                    next_operator,
                    next_hlr_status,
                    now_iso,
                    msg_id,
                )
            return True
        except _PG_CONN_ERRORS as exc:
            logger.debug(
                "PostgreSQL CDR update failed (%s); falling back to Redis",
                type(exc).__name__,
            )
            _discard_pg_pool(backoff=True)

    cdr_key = f"{CDR_KEY_PREFIX}{msg_id}"
    exists = await redis_client.exists(cdr_key)
    if not exists:
        return False

    updates: dict[str, str] = {
        "status": status,
        "updated_at": now_iso,
    }
    if msisdn:
        current_msisdn = await redis_client.hget(cdr_key, "msisdn")
        if not current_msisdn:
            updates["msisdn"] = msisdn
    if operator:
        updates["operator"] = operator
    if hlr_status:
        updates["hlr_status"] = hlr_status

    await redis_client.hset(cdr_key, mapping=updates)
    return True


async def topup_balance(
    redis_client: Redis,
    *,
    channel: str,
    amount_som: float,
) -> dict[str, float]:
    """Credit a telecom channel balance atomically and return updated balances."""
    amount = round(float(amount_som), 2)
    if amount <= 0:
        raise ValueError("Topup amount must be positive")
    now_iso = clock.now_iso()

    pool = await _get_pg_pool()
    if pool is not None:
        try:
            async with pool.acquire() as conn, conn.transaction():
                await _seed_pg_defaults(conn)
                if channel not in DEFAULT_BALANCES:
                    await conn.execute(
                        """
                            INSERT INTO sdp_accounts (channel, balance, updated_at)
                            VALUES ($1, $2, $3)
                            ON CONFLICT (channel) DO NOTHING
                            """,
                        channel,
                        Decimal("0.00"),
                        now_iso,
                    )
                await conn.execute(
                    """
                        UPDATE sdp_accounts
                        SET balance = ROUND(balance + $1, 2), updated_at = $2
                        WHERE channel = $3
                        """,
                    Decimal(f"{amount:.2f}"),
                    now_iso,
                    channel,
                )
                rows = await conn.fetch("SELECT channel, balance FROM sdp_accounts")
            return {row["channel"]: round(float(row["balance"]), 2) for row in rows}
        except _PG_CONN_ERRORS as exc:
            logger.debug("PostgreSQL topup failed (%s); falling back to Redis", type(exc).__name__)
            _discard_pg_pool(backoff=True)

    await _ensure_redis_balances(redis_client)
    topup_script = redis_client.register_script(_TOPUP_BALANCE_LUA)
    await topup_script(keys=[BALANCES_KEY], args=[channel, f"{amount:.2f}"])
    return await _ensure_redis_balances(redis_client)


async def reset_billing_state(redis_client: Redis) -> None:
    """Reset PostgreSQL SDP tables (if active) and remove all billing:* keys from Redis."""
    keys: list[str] = []
    async for key in redis_client.scan_iter(match="billing:*"):
        keys.append(key)
    if keys:
        await redis_client.delete(*keys)

    pool = await _get_pg_pool()
    if pool is not None:
        try:
            async with pool.acquire() as conn, conn.transaction():
                await conn.execute("TRUNCATE TABLE sdp_cdrs RESTART IDENTITY")
                await conn.execute("DELETE FROM sdp_accounts")
                await _seed_pg_defaults(conn)
        except _PG_CONN_ERRORS as exc:
            logger.debug("PostgreSQL reset failed (%s)", type(exc).__name__)
            _discard_pg_pool(backoff=True)


async def get_billing_snapshot(
    redis_client: Redis,
    limit: int = 50,
) -> dict[str, Any]:
    """Return current storage engine, account balances, recent CDRs, and HLR summary."""
    hlr_summary = await get_hlr_summary_async(redis_client)

    pool = await _get_pg_pool()
    if pool is not None:
        try:
            async with pool.acquire() as conn, conn.transaction():
                await _seed_pg_defaults(conn)
                acc_rows = await conn.fetch("SELECT channel, balance FROM sdp_accounts")
                balances = {
                    row["channel"]: round(float(row["balance"]), 2) for row in acc_rows
                }
                cdrs: list[dict[str, Any]] = []
                if limit > 0:
                    cdr_rows = await conn.fetch(
                        """
                            SELECT msg_id, msisdn, channel, operator, hlr_status,
                                   text_len, encoding, segments, cost_som, status, ts, updated_at
                            FROM sdp_cdrs
                            ORDER BY seq DESC
                            LIMIT $1
                            """,
                        limit,
                    )
                    for row in cdr_rows:
                        cdrs.append(
                            {
                                "msg_id": row["msg_id"],
                                "msisdn": row["msisdn"],
                                "channel": row["channel"],
                                "operator": row["operator"],
                                "hlr_status": row["hlr_status"],
                                "text_len": int(row["text_len"]),
                                "encoding": row["encoding"],
                                "segments": int(row["segments"]),
                                "cost_som": round(float(row["cost_som"]), 2),
                                "status": row["status"],
                                "ts": row["ts"],
                                "updated_at": row["updated_at"],
                            }
                        )
            return {
                "storage_engine": "PostgreSQL 18 (SDP-07 ACID)",
                "balances": balances,
                "cdrs": cdrs,
                "hlr": hlr_summary,
            }
        except _PG_CONN_ERRORS as exc:
            logger.debug(
                "PostgreSQL snapshot failed (%s); falling back to Redis", type(exc).__name__
            )
            _discard_pg_pool(backoff=True)

    balances = await _ensure_redis_balances(redis_client)
    if limit <= 0:
        return {
            "storage_engine": "Redis (Fallback)",
            "balances": balances,
            "cdrs": [],
            "hlr": hlr_summary,
        }

    cdr_ids = await redis_client.lrange(CDR_IDS_KEY, 0, limit - 1)
    if not cdr_ids:
        return {
            "storage_engine": "Redis (Fallback)",
            "balances": balances,
            "cdrs": [],
            "hlr": hlr_summary,
        }

    async with redis_client.pipeline(transaction=False) as pipe:
        for msg_id in cdr_ids:
            pipe.hgetall(f"{CDR_KEY_PREFIX}{msg_id}")
        raw_cdrs = await pipe.execute()

    cdrs = []
    for row in raw_cdrs:
        if not row:
            continue
        cdrs.append(
            {
                "msg_id": row.get("msg_id", ""),
                "msisdn": row.get("msisdn", ""),
                "channel": row.get("channel", ""),
                "operator": row.get("operator", ""),
                "hlr_status": row.get("hlr_status", "HLR_OK"),
                "text_len": int(row.get("text_len", 0) or 0),
                "encoding": row.get("encoding", "GSM-7"),
                "segments": int(row.get("segments", 1) or 1),
                "cost_som": round(float(row.get("cost_som", 0.0) or 0.0), 2),
                "status": row.get("status", "SUBMITTED"),
                "ts": row.get("ts", ""),
                "updated_at": row.get("updated_at", ""),
            }
        )

    return {
        "storage_engine": "Redis (Fallback)",
        "balances": balances,
        "cdrs": cdrs,
        "hlr": hlr_summary,
    }
