from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

TZ_BISHKEK = timezone(timedelta(hours=6))


class AppClock:
    """Thread-safe application clock supporting deterministic freeze and advance."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frozen_ms: int | None = None
        self._offset_ms: int = 0

    def _current_ms_locked(self) -> int:
        if self._frozen_ms is not None:
            return self._frozen_ms + self._offset_ms
        return int(time.time_ns() // 1_000_000) + self._offset_ms

    def now_ms(self) -> int:
        """Return current epoch timestamp in milliseconds."""
        with self._lock:
            return self._current_ms_locked()

    def now_iso(self) -> str:
        """Return current timestamp formatted as ISO-8601 in Asia/Bishkek (+06:00)."""
        return datetime.fromtimestamp(self.now_ms() / 1000.0, tz=TZ_BISHKEK).isoformat(
            timespec="seconds"
        )

    def freeze(self, epoch_ms: int | None = None) -> int:
        """Freeze clock at epoch_ms (or current wall-clock ms) and clear offset."""
        with self._lock:
            self._frozen_ms = (
                int(epoch_ms)
                if epoch_ms is not None
                else int(time.time_ns() // 1_000_000)
            )
            self._offset_ms = 0
            return self._frozen_ms

    def advance(self, seconds: float) -> int:
        """Advance clock by the given number of seconds, freezing first if live."""
        with self._lock:
            if self._frozen_ms is None:
                self._frozen_ms = int(time.time_ns() // 1_000_000)
            self._offset_ms += round(seconds * 1000)
            return self._current_ms_locked()

    def reset(self) -> None:
        """Restore live wall-clock time."""
        with self._lock:
            self._frozen_ms = None
            self._offset_ms = 0


clock = AppClock()
