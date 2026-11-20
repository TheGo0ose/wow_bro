"""Генерация кодов и их хеширование."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import Callable, Iterable, Iterator

CodeSource = Callable[[], str]


def secure_code() -> str:
    """6 цифр из криптостойкого генератора; 000000 допустим, ведущие нули сохраняются."""
    return f"{secrets.randbelow(1_000_000):06d}"


class SequenceCodeSource:
    """Контролируемый источник кодов для тестов: выдаёт коды из заданного списка."""

    def __init__(self, codes: Iterable[str]) -> None:
        self._it: Iterator[str] = iter(codes)

    def __call__(self) -> str:
        return next(self._it)


class CodeHasher:
    """HMAC-SHA256 с секретным ключом. В хранилище попадает только хеш.

    В сообщение кроме кода входит номер и идентификатор выдачи, поэтому
    одинаковые коды разных выдач дают разные хеши (нельзя сопоставить
    хеши между собой или посчитать таблицу на 10^6 вариантов без ключа).
    """

    def __init__(self, secret: bytes) -> None:
        if not secret:
            raise ValueError("секрет HMAC не может быть пустым")
        self._secret = secret

    def digest(self, msisdn: str, otp_id: str, code: str) -> bytes:
        msg = f"{msisdn}:{otp_id}:{code}".encode("ascii")
        return hmac.new(self._secret, msg, hashlib.sha256).digest()

    def matches(self, msisdn: str, otp_id: str, code: str, expected: bytes) -> bool:
        # compare_digest сравнивает за постоянное время — нет утечки по таймингу.
        return hmac.compare_digest(self.digest(msisdn, otp_id, code), expected)
