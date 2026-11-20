"""Имитация SMS-шлюза: каждая выдача дописывается строкой JSON в outbox/sms.jsonl.

Сервис этот файл никогда не читает — это «внешний» транспорт, из которого
жюри (и наши тесты) перехватывают код (capture_delivery).
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Protocol


class Delivery(Protocol):
    def deliver(self, msisdn: str, code: str, ts_iso: str) -> None: ...


class OutboxDelivery:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def deliver(self, msisdn: str, code: str, ts_iso: str) -> None:
        line = json.dumps({"ts": ts_iso, "msisdn": msisdn, "code": code}, ensure_ascii=False)
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()


class MemoryDelivery:
    """Перехватчик для unit-тестов."""

    def __init__(self) -> None:
        self.sent: list[dict[str, str]] = []

    def deliver(self, msisdn: str, code: str, ts_iso: str) -> None:
        self.sent.append({"ts": ts_iso, "msisdn": msisdn, "code": code})

    def last_code(self, msisdn: str) -> str:
        for item in reversed(self.sent):
            if item["msisdn"] == msisdn:
                return item["code"]
        raise LookupError(f"нет доставок для {msisdn}")


def read_last_code(outbox: Path, msisdn: str) -> str:
    """Последний код для номера из outbox (используют только тесты и сценарии)."""
    last = None
    with Path(outbox).open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if item.get("msisdn") == msisdn:
                last = item["code"]
    if last is None:
        raise LookupError(f"нет доставок для {msisdn}")
    return last
