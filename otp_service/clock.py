"""Часы. Реальные — для боевого режима, подменяемые — для тестов и сценариев жюри."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

# Asia/Bishkek = UTC+6 без перехода на летнее время.
BISHKEK_TZ = timezone(timedelta(hours=6))


class Clock:
    def now(self) -> float:
        """Текущее время в секундах (Unix epoch)."""
        raise NotImplementedError

    def iso(self, ts: float | None = None) -> str:
        """Время в ISO 8601 по Бишкеку, например 2026-11-21T09:00:00+06:00."""
        value = self.now() if ts is None else ts
        dt = datetime.fromtimestamp(value, tz=BISHKEK_TZ)
        if dt.microsecond:
            return dt.isoformat(timespec="milliseconds")
        return dt.isoformat(timespec="seconds")


class SystemClock(Clock):
    def now(self) -> float:
        return time.time()


class FakeClock(Clock):
    """Часы, которые идут только по команде advance()."""

    def __init__(self, start: float | str = 0.0) -> None:
        self._lock = threading.Lock()
        self._start = self._parse(start)
        self._now = self._start

    @staticmethod
    def _parse(start: float | str) -> float:
        if isinstance(start, str):
            return datetime.fromisoformat(start).timestamp()
        return float(start)

    def now(self) -> float:
        with self._lock:
            return self._now

    def advance(self, seconds: float) -> float:
        if seconds < 0:
            raise ValueError("время не может идти назад")
        with self._lock:
            self._now += seconds
            return self._now

    def reset(self, start: float | str | None = None) -> None:
        with self._lock:
            if start is not None:
                self._start = self._parse(start)
            self._now = self._start
