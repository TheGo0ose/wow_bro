from __future__ import annotations

import asyncio
import math
import os
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import asyncpg

from infra.hlr import get_hlr_summary, seed_hlr_overrides

TZ_BISHKEK = timezone(timedelta(hours=6))

CHANNEL_RATES_SOM: dict[str, float] = {
    "otp_svc": 0.45,
    "bulk_mkt": 0.30,
}

DEFAULT_BALANCES: dict[str, float] = {
    "otp_svc": 5000.00,
    "bulk_mkt": 1000.00,
}

_GSM7_BASIC = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ ÆæßÉ"
    " !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmnopqrstuvwxyzäöñüà"
)
_GSM7_EXTENDED = frozenset("^{}\\[~]|€")

_DDL_STATEMENTS = """
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

CREATE INDEX IF NOT EXISTS idx_sdp_cdrs_seq_desc ON sdp_cdrs (seq DESC);

CREATE TABLE IF NOT EXISTS hlr_overrides (
    msisdn VARCHAR(32) PRIMARY KEY,
    target_operator VARCHAR(32),
    in_network BOOLEAN,
    updated_at TEXT NOT NULL
);
"""

_CDR_COLUMNS = (
    "msg_id, msisdn, channel, operator, hlr_status, "
    "text_len, encoding, segments, cost_som, status, ts, updated_at"
)


class InsufficientBalanceError(Exception):
    """Raised when a telecom account lacks funds for an SMS charge."""

    def __init__(self, channel: str, balance: float, required: float) -> None:
        self.channel = channel
        self.balance = round(float(balance), 2)
        self.required = round(float(required), 2)
        super().__init__(
            f"Insufficient balance for {channel}: "
            f"balance={self.balance:.2f}, required={self.required:.2f}"
        )


def _now_iso() -> str:
    return datetime.now(tz=TZ_BISHKEK).isoformat(timespec="seconds")


def _to_decimal(value: float) -> Decimal:
    return Decimal(f"{round(float(value), 2):.2f}")


def count_sms_segments(text: str) -> tuple[str, int]:
    """Return (encoding, segment_count) using GSM-7 (160/153) or UCS-2 (70/67)."""
    if not text:
        return ("GSM-7", 1)

    septets = 0
    is_gsm7 = True
    for ch in text:
        if ch in _GSM7_BASIC:
            septets += 1
        elif ch in _GSM7_EXTENDED:
            septets += 2
        else:
            is_gsm7 = False
            break

    if is_gsm7:
        if septets <= 160:
            return ("GSM-7", 1)
        return ("GSM-7", math.ceil(septets / 153))

    length = len(text)
    if length <= 70:
        return ("UCS-2", 1)
    return ("UCS-2", math.ceil(length / 67))


def _row_to_cdr(row: asyncpg.Record) -> dict[str, Any]:
    return {
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


class BillingRepository:
    """SDP-07 billing & CDR repository backed by PostgreSQL or in-memory state."""

    def __init__(self, database_url: str | None = None) -> None:
        raw_url = os.environ.get("DATABASE_URL", "") if database_url is None else database_url
        self.database_url: str = raw_url.strip()
        self._pool: asyncpg.Pool | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._init_lock: asyncio.Lock | None = None

        self._mem_lock: asyncio.Lock | None = None
        self._mem_balances: dict[str, float] = dict(DEFAULT_BALANCES)
        self._mem_cdrs: list[dict[str, Any]] = []
        self._mem_cdr_index: dict[str, dict[str, Any]] = {}

    @property
    def storage_engine(self) -> str:
        """Return human-readable active storage engine label."""
        if self.database_url:
            return "PostgreSQL 18 (SDP-07 ACID)"
        return "In-Memory SDP (Test Mode)"

    def _check_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            self._pool = None
            self._init_lock = None
            self._mem_lock = None
        self._loop = loop
        return loop

    def _get_mem_lock(self) -> asyncio.Lock:
        self._check_loop()
        if self._mem_lock is None:
            self._mem_lock = asyncio.Lock()
        return self._mem_lock

    async def _ensure_pool(self) -> asyncpg.Pool:
        self._check_loop()
        if self._init_lock is None:
            self._init_lock = asyncio.Lock()

        async with self._init_lock:
            if self._pool is None:
                pool = await asyncpg.create_pool(self.database_url, min_size=1, max_size=10)
                try:
                    await self._setup_schema_and_seed(pool)
                except Exception:
                    await pool.close()
                    raise
                self._pool = pool
            return self._pool

    async def _setup_schema_and_seed(self, pool: asyncpg.Pool) -> None:
        now_ts = _now_iso()
        async with pool.acquire() as conn:
            await conn.execute(_DDL_STATEMENTS)
            await conn.execute(
                """
                INSERT INTO sdp_accounts (channel, balance, updated_at)
                VALUES ('otp_svc', $1, $3), ('bulk_mkt', $2, $3)
                ON CONFLICT (channel) DO NOTHING
                """,
                _to_decimal(DEFAULT_BALANCES["otp_svc"]),
                _to_decimal(DEFAULT_BALANCES["bulk_mkt"]),
                now_ts,
            )
            rows = await conn.fetch(
                "SELECT msisdn, target_operator, in_network FROM hlr_overrides"
            )
        seed_hlr_overrides(
            [(r["msisdn"], r["target_operator"], r["in_network"]) for r in rows]
        )

    async def init(self) -> None:
        """Ensure schema, seed balances, and load persisted HLR overrides."""
        if not self.database_url:
            async with self._get_mem_lock():
                for ch, bal in DEFAULT_BALANCES.items():
                    self._mem_balances.setdefault(ch, bal)
            return

        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT msisdn, target_operator, in_network FROM hlr_overrides"
            )
        seed_hlr_overrides(
            [(r["msisdn"], r["target_operator"], r["in_network"]) for r in rows]
        )

    async def charge_and_create_cdr(
        self,
        *,
        msg_id: str,
        msisdn: str,
        channel: str,
        text: str,
        operator: str,
        hlr_status: str,
        status: str = "SUBMITTED",
        enforce_balance: bool = True,
        cost_override: float | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically charge account and insert CDR; idempotent on msg_id."""
        if channel not in CHANNEL_RATES_SOM:
            raise ValueError(f"Unsupported channel: {channel}")

        encoding, segments = count_sms_segments(text)
        cost_som = (
            round(float(cost_override), 2)
            if cost_override is not None
            else round(CHANNEL_RATES_SOM[channel] * segments, 2)
        )
        now_ts = _now_iso()

        if not self.database_url:
            async with self._get_mem_lock():
                existing = self._mem_cdr_index.get(msg_id)
                if existing is not None:
                    return (dict(existing), False)

                bal = float(self._mem_balances.get(channel, DEFAULT_BALANCES[channel]))
                if enforce_balance and bal < cost_som:
                    raise InsufficientBalanceError(channel, bal, cost_som)

                if cost_som > 0:
                    self._mem_balances[channel] = round(bal - cost_som, 2)

                cdr = {
                    "msg_id": msg_id,
                    "msisdn": msisdn,
                    "channel": channel,
                    "operator": operator,
                    "hlr_status": hlr_status,
                    "text_len": len(text),
                    "encoding": encoding,
                    "segments": segments,
                    "cost_som": cost_som,
                    "status": status,
                    "ts": now_ts,
                    "updated_at": now_ts,
                }
                self._mem_cdrs.append(cdr)
                self._mem_cdr_index[msg_id] = cdr
                return (dict(cdr), True)

        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.transaction():
            existing = await conn.fetchrow(
                f"SELECT {_CDR_COLUMNS} FROM sdp_cdrs WHERE msg_id = $1",
                msg_id,
            )
            if existing is not None:
                return (_row_to_cdr(existing), False)

            acct_row = await conn.fetchrow(
                "SELECT balance FROM sdp_accounts WHERE channel = $1 FOR UPDATE",
                channel,
            )
            if acct_row is None:
                default_bal = DEFAULT_BALANCES[channel]
                await conn.execute(
                    """
                    INSERT INTO sdp_accounts (channel, balance, updated_at)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (channel) DO NOTHING
                    """,
                    channel,
                    _to_decimal(default_bal),
                    now_ts,
                )
                acct_row = await conn.fetchrow(
                    "SELECT balance FROM sdp_accounts WHERE channel = $1 FOR UPDATE",
                    channel,
                )

            existing_after_lock = await conn.fetchrow(
                f"SELECT {_CDR_COLUMNS} FROM sdp_cdrs WHERE msg_id = $1",
                msg_id,
            )
            if existing_after_lock is not None:
                return (_row_to_cdr(existing_after_lock), False)

            bal = float(acct_row["balance"]) if acct_row is not None else 0.0
            if enforce_balance and bal < cost_som:
                raise InsufficientBalanceError(channel, bal, cost_som)

            if cost_som > 0:
                new_bal = round(bal - cost_som, 2)
                await conn.execute(
                    "UPDATE sdp_accounts SET balance = $1, updated_at = $2 WHERE channel = $3",
                    _to_decimal(new_bal),
                    now_ts,
                    channel,
                )

            inserted = await conn.fetchrow(
                f"""
                INSERT INTO sdp_cdrs ({_CDR_COLUMNS})
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (msg_id) DO NOTHING
                RETURNING {_CDR_COLUMNS}
                """,
                msg_id,
                msisdn,
                channel,
                operator,
                hlr_status,
                len(text),
                encoding,
                segments,
                _to_decimal(cost_som),
                status,
                now_ts,
                now_ts,
            )
            if inserted is None:
                row = await conn.fetchrow(
                    f"SELECT {_CDR_COLUMNS} FROM sdp_cdrs WHERE msg_id = $1",
                    msg_id,
                )
                assert row is not None
                return (_row_to_cdr(row), False)
            return (_row_to_cdr(inserted), True)

    async def charge_bulk_batch(
        self,
        *,
        items: list[dict[str, Any]],
        text: str,
    ) -> dict[str, Any]:
        """Charge an entire bulk_mkt batch in one ACID transaction and record CDRs."""
        encoding, segments = count_sms_segments(text)
        per_msg_cost = round(CHANNEL_RATES_SOM["bulk_mkt"] * segments, 2)
        total_cost = round(per_msg_cost * len(items), 2)
        now_ts = _now_iso()

        if not self.database_url:
            async with self._get_mem_lock():
                bal = float(self._mem_balances.get("bulk_mkt", DEFAULT_BALANCES["bulk_mkt"]))
                if bal < total_cost:
                    raise InsufficientBalanceError("bulk_mkt", bal, total_cost)
                new_bal = round(bal - total_cost, 2)
                self._mem_balances["bulk_mkt"] = new_bal

                for item in items:
                    msg_id = str(item["msg_id"])
                    if msg_id in self._mem_cdr_index:
                        continue
                    cdr = {
                        "msg_id": msg_id,
                        "msisdn": str(item["msisdn"]),
                        "channel": "bulk_mkt",
                        "operator": str(item.get("operator") or ""),
                        "hlr_status": str(item.get("hlr_status") or "HLR_OK"),
                        "text_len": len(text),
                        "encoding": encoding,
                        "segments": segments,
                        "cost_som": per_msg_cost,
                        "status": str(item.get("status") or "SUBMITTED"),
                        "ts": now_ts,
                        "updated_at": now_ts,
                    }
                    self._mem_cdrs.append(cdr)
                    self._mem_cdr_index[msg_id] = cdr

                return {
                    "encoding": encoding,
                    "segments": segments,
                    "total_cost": total_cost,
                    "balance": new_bal,
                }

        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.transaction():
            acct_row = await conn.fetchrow(
                "SELECT balance FROM sdp_accounts WHERE channel = 'bulk_mkt' FOR UPDATE"
            )
            bal = float(acct_row["balance"]) if acct_row is not None else 0.0
            if bal < total_cost:
                raise InsufficientBalanceError("bulk_mkt", bal, total_cost)

            new_bal = round(bal - total_cost, 2)
            await conn.execute(
                """
                UPDATE sdp_accounts
                SET balance = $1, updated_at = $2
                WHERE channel = 'bulk_mkt'
                """,
                _to_decimal(new_bal),
                now_ts,
            )

            if items:
                cost_dec = _to_decimal(per_msg_cost)
                records = [
                    (
                        str(item["msg_id"]),
                        str(item["msisdn"]),
                        "bulk_mkt",
                        str(item.get("operator") or ""),
                        str(item.get("hlr_status") or "HLR_OK"),
                        len(text),
                        encoding,
                        segments,
                        cost_dec,
                        str(item.get("status") or "SUBMITTED"),
                        now_ts,
                        now_ts,
                    )
                    for item in items
                ]
                await conn.executemany(
                    f"""
                    INSERT INTO sdp_cdrs ({_CDR_COLUMNS})
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    ON CONFLICT (msg_id) DO NOTHING
                    """,
                    records,
                )

            return {
                "encoding": encoding,
                "segments": segments,
                "total_cost": total_cost,
                "balance": new_bal,
            }

    async def update_cdr_status(
        self,
        *,
        msg_id: str,
        status: str,
        operator: str = "",
        hlr_status: str = "",
    ) -> bool:
        """Update delivery status (and optionally operator/HLR) on an existing CDR."""
        now_ts = _now_iso()

        if not self.database_url:
            async with self._get_mem_lock():
                cdr = self._mem_cdr_index.get(msg_id)
                if cdr is None:
                    return False
                cdr["status"] = status
                if operator:
                    cdr["operator"] = operator
                if hlr_status:
                    cdr["hlr_status"] = hlr_status
                cdr["updated_at"] = now_ts
                return True

        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            result = await conn.execute(
                """
                UPDATE sdp_cdrs
                SET status = $2,
                    operator = CASE WHEN $3 <> '' THEN $3 ELSE operator END,
                    hlr_status = CASE WHEN $4 <> '' THEN $4 ELSE hlr_status END,
                    updated_at = $5
                WHERE msg_id = $1
                """,
                msg_id,
                status,
                operator,
                hlr_status,
                now_ts,
            )
        return result != "UPDATE 0"

    async def topup_balance(self, *, channel: str, amount_som: float) -> dict[str, float]:
        """Credit a telecom channel balance atomically and return updated balances."""
        if channel not in CHANNEL_RATES_SOM:
            raise ValueError(f"Unsupported channel: {channel}")
        delta = round(float(amount_som), 2)
        if delta <= 0:
            raise ValueError("Top-up amount must be positive")
        now_ts = _now_iso()

        if not self.database_url:
            async with self._get_mem_lock():
                cur = float(self._mem_balances.get(channel, DEFAULT_BALANCES[channel]))
                self._mem_balances[channel] = round(cur + delta, 2)
                return {
                    ch: round(float(self._mem_balances.get(ch, DEFAULT_BALANCES[ch])), 2)
                    for ch in DEFAULT_BALANCES
                }

        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(
                """
                INSERT INTO sdp_accounts (channel, balance, updated_at)
                VALUES ($1, $2, $3)
                ON CONFLICT (channel) DO UPDATE
                SET balance = sdp_accounts.balance + $4, updated_at = $3
                """,
                channel,
                _to_decimal(DEFAULT_BALANCES[channel] + delta),
                now_ts,
                _to_decimal(delta),
            )
            rows = await conn.fetch("SELECT channel, balance FROM sdp_accounts")
        return {row["channel"]: round(float(row["balance"]), 2) for row in rows}

    async def save_hlr_override(
        self,
        *,
        msisdn: str,
        operator: str | None = None,
        in_network: bool | None = None,
    ) -> None:
        """Persist an HLR/MNP override in PostgreSQL (no-op in in-memory mode)."""
        if not self.database_url:
            return
        now_ts = _now_iso()
        clean_op = operator.strip() if operator is not None and operator.strip() else None

        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO hlr_overrides (msisdn, target_operator, in_network, updated_at)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (msisdn) DO UPDATE
                SET target_operator = COALESCE(EXCLUDED.target_operator, hlr_overrides.target_operator),
                    in_network = COALESCE(EXCLUDED.in_network, hlr_overrides.in_network),
                    updated_at = EXCLUDED.updated_at
                """,
                msisdn,
                clean_op,
                in_network,
                now_ts,
            )

    async def reset_all(self) -> None:
        """Reset channel balances to defaults and clear all CDRs and HLR overrides."""
        now_ts = _now_iso()
        if not self.database_url:
            async with self._get_mem_lock():
                self._mem_balances = dict(DEFAULT_BALANCES)
                self._mem_cdrs.clear()
                self._mem_cdr_index.clear()
            return

        pool = await self._ensure_pool()
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute("TRUNCATE TABLE sdp_cdrs, hlr_overrides RESTART IDENTITY")
            await conn.execute(
                """
                INSERT INTO sdp_accounts (channel, balance, updated_at)
                VALUES ('otp_svc', $1, $3), ('bulk_mkt', $2, $3)
                ON CONFLICT (channel) DO UPDATE
                SET balance = EXCLUDED.balance, updated_at = EXCLUDED.updated_at
                """,
                _to_decimal(DEFAULT_BALANCES["otp_svc"]),
                _to_decimal(DEFAULT_BALANCES["bulk_mkt"]),
                now_ts,
            )

    async def get_snapshot(self, limit: int = 50) -> dict[str, Any]:
        """Return storage engine info, channel balances, recent CDRs, and HLR summary."""
        safe_limit = max(1, min(int(limit), 500))

        if not self.database_url:
            async with self._get_mem_lock():
                balances = {
                    ch: round(float(self._mem_balances.get(ch, DEFAULT_BALANCES[ch])), 2)
                    for ch in DEFAULT_BALANCES
                }
                cdrs = [dict(c) for c in reversed(self._mem_cdrs[-safe_limit:])]
            return {
                "storage_engine": self.storage_engine,
                "balances": balances,
                "cdrs": cdrs,
                "hlr": get_hlr_summary(),
            }

        pool = await self._ensure_pool()
        async with pool.acquire() as conn:
            acct_rows = await conn.fetch("SELECT channel, balance FROM sdp_accounts")
            cdr_rows = await conn.fetch(
                f"SELECT {_CDR_COLUMNS} FROM sdp_cdrs ORDER BY seq DESC LIMIT $1",
                safe_limit,
            )

        balances = {row["channel"]: round(float(row["balance"]), 2) for row in acct_rows}
        for ch, default_bal in DEFAULT_BALANCES.items():
            balances.setdefault(ch, default_bal)

        return {
            "storage_engine": self.storage_engine,
            "balances": balances,
            "cdrs": [_row_to_cdr(r) for r in cdr_rows],
            "hlr": get_hlr_summary(),
        }

    async def close(self) -> None:
        """Close the PostgreSQL connection pool cleanly."""
        if self._pool is not None:
            pool = self._pool
            self._pool = None
            self._loop = None
            with suppress(asyncpg.PostgresError, OSError, RuntimeError):
                await pool.close()
