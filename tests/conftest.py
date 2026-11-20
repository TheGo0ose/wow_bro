from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

# Ensure deterministic test defaults before importing settings/app.
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("TELECOM_ENABLED", "false")
os.environ.setdefault("HMAC_SECRET", "test-secret-2026")

from app.config import get_settings
from app.core.clock import clock
from app.core.redis_client import close_redis, create_redis_client, set_redis_client
from app.main import app

FIXED_TEST_EPOCH_MS = 1_795_190_400_000


@pytest_asyncio.fixture
async def clean_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[Any]:
    """Provide an isolated Redis DB, temporary outbox path, and frozen clock."""
    monkeypatch.setenv("TELECOM_ENABLED", "false")
    monkeypatch.setenv("HMAC_SECRET", "test-secret-2026")
    monkeypatch.setenv("OTP_OUTBOX", str(tmp_path / "sms.jsonl"))
    get_settings.cache_clear()

    redis = create_redis_client()
    await redis.flushdb()
    set_redis_client(redis)
    clock.freeze(FIXED_TEST_EPOCH_MS)

    try:
        yield redis
    finally:
        clock.reset()
        await close_redis()
        get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(clean_env: Any) -> AsyncIterator[httpx.AsyncClient]:
    """Return an async HTTPX client wired directly to the FastAPI ASGI app."""
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as async_client:
        yield async_client
