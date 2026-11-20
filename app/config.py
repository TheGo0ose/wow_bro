from __future__ import annotations

import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

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
    DATABASE_URL: str = "postgresql://otp:otp_secret@localhost:5432/otp_billing"


_overrides_lock = threading.Lock()
_runtime_overrides: dict[str, Any] = {}

_OVERRIDE_KEY_MAP: dict[str, str] = {
    "otp_ttl": "OTP_TTL",
    "otp_ttl_seconds": "OTP_TTL",
    "otp_key_ttl": "OTP_KEY_TTL",
    "otp_key_ttl_seconds": "OTP_KEY_TTL",
    "lockout_ttl": "LOCKOUT_TTL",
    "lockout_seconds": "LOCKOUT_TTL",
    "lockout_ttl_seconds": "LOCKOUT_TTL",
    "max_verify_attempts": "MAX_VERIFY_ATTEMPTS",
    "max_attempts": "MAX_VERIFY_ATTEMPTS",
    "ip_limit_per_min": "IP_LIMIT_PER_MIN",
    "ip_limit": "IP_LIMIT_PER_MIN",
    "ip_window_seconds": "IP_WINDOW_SECONDS",
    "send_limit_10m": "SEND_LIMIT_10M",
    "send_limit": "SEND_LIMIT_10M",
    "send_window_seconds": "SEND_WINDOW_SECONDS",
    "cooldowns": "COOLDOWNS",
    "database_url": "DATABASE_URL",
}


@lru_cache
def get_base_settings() -> Settings:
    """Return cached base application settings loaded from env/.env."""
    return Settings()


def get_settings() -> Settings:
    """Return application settings with any active runtime overrides applied."""
    base = get_base_settings()
    with _overrides_lock:
        overrides = dict(_runtime_overrides)
    if overrides:
        return base.model_copy(update=overrides)
    return base


get_settings.cache_clear = get_base_settings.cache_clear


def set_runtime_overrides(overrides: dict[str, Any] | None) -> dict[str, Any]:
    """Replace runtime settings overrides using case-insensitive field/alias mapping."""
    normalized: dict[str, Any] = {}
    if overrides:
        valid_fields = Settings.model_fields
        for raw_key, value in overrides.items():
            if value is None:
                continue
            key_str = str(raw_key).strip()
            mapped_key = _OVERRIDE_KEY_MAP.get(key_str.lower())
            if mapped_key is None:
                if key_str in valid_fields:
                    mapped_key = key_str
                elif key_str.upper() in valid_fields:
                    mapped_key = key_str.upper()
            if mapped_key is not None and mapped_key in valid_fields:
                normalized[mapped_key] = value

    with _overrides_lock:
        _runtime_overrides.clear()
        _runtime_overrides.update(normalized)
        return dict(_runtime_overrides)


def clear_runtime_overrides() -> None:
    """Clear all active runtime settings overrides."""
    with _overrides_lock:
        _runtime_overrides.clear()


def resolve_outbox_path() -> Path:
    """Resolve the SMS outbox path, honoring runtime OTP_OUTBOX env overrides."""
    raw = os.environ.get("OTP_OUTBOX") or get_settings().OTP_OUTBOX
    return Path(raw)
