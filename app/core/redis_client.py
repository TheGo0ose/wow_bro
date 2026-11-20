from __future__ import annotations

import redis.asyncio as aioredis
from redis.asyncio import Redis

from app.config import get_settings

_redis_client: Redis | None = None


def create_redis_client(url: str | None = None) -> Redis:
    """Create a new async Redis client configured with decode_responses=True."""
    target_url = url or get_settings().REDIS_URL
    return aioredis.from_url(target_url, decode_responses=True)


def set_redis_client(client: Redis | None) -> None:
    """Override the module-level async Redis client instance."""
    global _redis_client
    _redis_client = client


def get_redis() -> Redis:
    """Return the shared async Redis client, creating it lazily if unset."""
    global _redis_client
    if _redis_client is None:
        _redis_client = create_redis_client()
    return _redis_client


async def close_redis() -> None:
    """Close and clear the shared async Redis client if initialized."""
    global _redis_client
    if _redis_client is not None:
        client = _redis_client
        _redis_client = None
        await client.aclose()
