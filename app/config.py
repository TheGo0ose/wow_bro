from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables and .env."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    HMAC_SECRET: str = "otp-shield-secret-key-2026"
    REDIS_URL: str = "redis://localhost:6379/0"
    OTP_OUTBOX: str = "outbox/sms.jsonl"
    OTP_TTL: int = 120
    OTP_KEY_TTL: int = 3600
    LOCKOUT_TTL: int = 900
    MAX_VERIFY_ATTEMPTS: int = 5
    IP_LIMIT_PER_MIN: int = 20
    IP_WINDOW_SECONDS: int = 60
    SEND_LIMIT_10M: int = 3
    SEND_WINDOW_SECONDS: int = 600
    COOLDOWNS: list[int] = [30, 60, 120]
    TELECOM_GATEWAY_URL: str = "http://localhost:1401"
    TELECOM_ENABLED: str = "auto"
    TEST_MODE: bool = False


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings."""
    return Settings()


def resolve_outbox_path() -> Path:
    """Resolve the SMS outbox path, honoring runtime OTP_OUTBOX env overrides."""
    raw = os.environ.get("OTP_OUTBOX") or get_settings().OTP_OUTBOX
    return Path(raw)
