"""Настройки сервиса. Все значения читаются из переменных окружения."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    return float(raw) if raw not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


@dataclass
class Settings:
    # --- секрет HMAC ---
    # Если OTP_SECRET не задан, генерируется случайный ключ на время жизни процесса.
    secret: bytes = field(default_factory=lambda: secrets.token_bytes(32))

    # --- параметры OTP (значения по ТЗ) ---
    code_ttl: float = 120.0                     # срок жизни кода, с
    send_window: float = 600.0                  # окно лимита выдачи, с (10 минут)
    send_max_in_window: int = 3                 # не больше 3 send за окно
    send_delays: tuple[float, ...] = (30.0, 60.0, 120.0)  # задержка после 1-й, 2-й, 3-й выдачи
    max_failures: int = 5                       # неверных вводов до блокировки
    lock_duration: float = 900.0                # блокировка номера, с (15 минут)
    ip_window: float = 60.0                     # окно IP-лимита verify, с
    ip_max_in_window: int = 20                  # не больше 20 verify с одного IP за окно

    # --- файлы ---
    outbox_path: Path = REPO_ROOT / "outbox" / "sms.jsonl"
    security_log_path: Path = REPO_ROOT / "logs" / "security.jsonl"

    # --- сеть / IP ---
    # Имя заголовка с IP клиента, которому доверяем ТОЛЬКО если соединение
    # пришло от доверенного прокси (по умолчанию выключено).
    trusted_ip_header: str = ""
    trusted_proxies: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})

    # --- тестовый режим ---
    # Подменяемые часы + служебные эндпоинты /__test__/* (только с loopback).
    test_mode: bool = False
    test_start_time: str = "2026-11-20T12:00:00+06:00"

    dashboard_enabled: bool = True

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        secret = os.environ.get("OTP_SECRET")
        if secret:
            s.secret = secret.encode("utf-8")
        s.code_ttl = _env_float("OTP_CODE_TTL", s.code_ttl)
        s.send_window = _env_float("OTP_SEND_WINDOW", s.send_window)
        s.send_max_in_window = _env_int("OTP_SEND_MAX", s.send_max_in_window)
        delays = os.environ.get("OTP_SEND_DELAYS")
        if delays:
            s.send_delays = tuple(float(x) for x in delays.split(","))
        s.max_failures = _env_int("OTP_MAX_FAILURES", s.max_failures)
        s.lock_duration = _env_float("OTP_LOCK_DURATION", s.lock_duration)
        s.ip_window = _env_float("OTP_IP_WINDOW", s.ip_window)
        s.ip_max_in_window = _env_int("OTP_IP_MAX", s.ip_max_in_window)

        outbox = os.environ.get("OTP_OUTBOX")
        if outbox:
            s.outbox_path = Path(outbox)
        log = os.environ.get("OTP_SECURITY_LOG")
        if log:
            s.security_log_path = Path(log)

        s.trusted_ip_header = os.environ.get("OTP_TRUSTED_IP_HEADER", "").strip()
        proxies = os.environ.get("OTP_TRUSTED_PROXIES")
        if proxies:
            s.trusted_proxies = frozenset(p.strip() for p in proxies.split(",") if p.strip())

        s.test_mode = _env_bool("OTP_TEST_MODE", False)
        s.test_start_time = os.environ.get("OTP_TEST_START_TIME", s.test_start_time)
        s.dashboard_enabled = _env_bool("OTP_DASHBOARD", True)
        return s
