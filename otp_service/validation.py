"""Проверка формата входных данных."""

from __future__ import annotations

import re
from typing import Any

_MSISDN_RE = re.compile(r"996\d{9}")
_CODE_RE = re.compile(r"\d{6}")


class ValidationError(ValueError):
    pass


def _is_ascii_digits_match(pattern: re.Pattern[str], value: Any) -> bool:
    # isascii(): \d в Python принимает и «арабские»/юникод-цифры — их отсекаем.
    return isinstance(value, str) and value.isascii() and pattern.fullmatch(value) is not None


def parse_msisdn(payload: Any) -> str:
    if not isinstance(payload, dict):
        raise ValidationError("тело запроса должно быть JSON-объектом")
    msisdn = payload.get("msisdn")
    if not _is_ascii_digits_match(_MSISDN_RE, msisdn):
        raise ValidationError("msisdn — строка ровно из 12 цифр, начинается с 996")
    return msisdn


def parse_verify(payload: Any) -> tuple[str, str]:
    msisdn = parse_msisdn(payload)
    code = payload.get("code")
    if not _is_ascii_digits_match(_CODE_RE, code):
        raise ValidationError("code — строка ровно из 6 цифр")
    return msisdn, code
