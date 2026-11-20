"""Журнал событий безопасности (JSONL). Никогда не содержит самих кодов."""

from __future__ import annotations

import json
import threading
from collections import Counter, deque
from pathlib import Path
from typing import Any

# Поля, которые запрещено писать в журнал ни при каких обстоятельствах.
_FORBIDDEN_KEYS = {"code", "otp", "otp_code", "secret", "hash"}


class SecurityLog:
    def __init__(self, path: Path | None, keep_in_memory: int = 500) -> None:
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._recent: deque[dict[str, Any]] = deque(maxlen=keep_in_memory)
        self._counts: Counter[str] = Counter()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, ts_iso: str, event: str, **fields: Any) -> None:
        leaked = _FORBIDDEN_KEYS.intersection(fields)
        if leaked:
            raise ValueError(f"в журнал безопасности нельзя писать: {sorted(leaked)}")
        record = {"ts": ts_iso, "event": event, **fields}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._recent.append(record)
            self._counts[event] += 1
            if self.path:
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")

    def recent(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            items = list(self._recent)
        return items[-limit:][::-1]

    def counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self._counts)

    def clear_memory(self) -> None:
        with self._lock:
            self._recent.clear()
            self._counts.clear()
