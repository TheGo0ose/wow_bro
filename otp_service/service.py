"""Бизнес-логика OTP. Не зависит ни от HTTP, ни от конкретного хранилища.

Состояние (коды, окна лимитов, блокировки) живёт в Store: в памяти процесса
или в Redis. Каждая операция хранилища атомарна, а проверка кода устроена как
fetch -> сравнение HMAC за постоянное время -> consume/fail по otp_id
(compare-and-set). Поэтому из N параллельных verify с верным кодом успешен
ровно один — и для одного процесса, и для нескольких реплик над общим Redis.
"""

from __future__ import annotations

import math
import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

from .clock import Clock
from .codes import CodeHasher, CodeSource, secure_code
from .config import Settings
from .delivery import Delivery
from .security_log import SecurityLog
from .storage import MemoryStore, Store
from .validation import ValidationError, parse_msisdn, parse_verify


@dataclass
class Result:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


def mask_msisdn(msisdn: str) -> str:
    """996770730000 -> 996770***000 (для журнала и дашборда)."""
    return msisdn[:6] + "***" + msisdn[-3:] if len(msisdn) == 12 else "***"


def _retry(status: int, error: str, wait: float) -> Result:
    seconds = max(1, math.ceil(wait))
    return Result(status, {"error": error, "retry_after": seconds}, {"Retry-After": str(seconds)})


class OtpService:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        delivery: Delivery,
        security_log: SecurityLog,
        code_source: CodeSource = secure_code,
        avoid_repeat_code: bool = False,
        store: Store | None = None,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.delivery = delivery
        self.log = security_log
        self.code_source = code_source
        self.store: Store = store or MemoryStore(settings.limits())
        self.hasher = CodeHasher(settings.secret)
        # В тестовом режиме новый код гарантированно отличается от предыдущего,
        # чтобы сценарии «новый send аннулирует старый код» были детерминированы.
        self.avoid_repeat_code = avoid_repeat_code
        self._prev_lock = threading.Lock()
        self._prev_hash: dict[str, bytes] = {}

    # ------------------------------------------------------------------ utils
    def reset(self) -> None:
        self.store.reset()
        with self._prev_lock:
            self._prev_hash.clear()

    def _event(self, now: float, event: str, msisdn: str | None = None, **fields: Any) -> None:
        if msisdn is not None:
            fields["msisdn"] = mask_msisdn(msisdn)
        self.log.write(self.clock.iso(now), event, **fields)

    def _new_code(self, msisdn: str) -> str:
        code = self.code_source()
        if not self.avoid_repeat_code:
            return code
        with self._prev_lock:
            prev = self._prev_hash.get(msisdn)
        for _ in range(10):
            if prev is None or not self.hasher.matches(msisdn, "prev", code, prev):
                break
            code = self.code_source()
        return code

    def _remember_code(self, msisdn: str, code: str) -> None:
        if self.avoid_repeat_code:
            with self._prev_lock:
                self._prev_hash[msisdn] = self.hasher.digest(msisdn, "prev", code)

    # ------------------------------------------------------------------- send
    def send(self, payload: Any, client_ip: str | None = None) -> Result:
        try:
            msisdn = parse_msisdn(payload)
        except ValidationError as exc:
            return Result(400, {"error": "bad_request", "detail": str(exc)})

        s = self.settings
        now = self.clock.now()
        otp_id = secrets.token_hex(8)
        code = self._new_code(msisdn)
        res = self.store.issue(msisdn, now, otp_id, self.hasher.digest(msisdn, otp_id, code))

        if res.status == "locked":
            self._event(now, "send_rejected_locked", msisdn, ip=client_ip)
            return _retry(423, "locked", res.wait)
        if res.status == "window_full":
            self._event(now, "send_rate_limited", msisdn, ip=client_ip,
                        reason="window_full", sends_in_window=res.sends_in_window)
            return _retry(429, "too_many_requests", res.wait)
        if res.status == "delay":
            self._event(now, "send_rate_limited", msisdn, ip=client_ip,
                        reason="resend_delay", required_delay=res.delay)
            return _retry(429, "too_many_requests", res.wait)

        # Код уходит только в «SMS-шлюз»; в хранилище — лишь его HMAC.
        self._remember_code(msisdn, code)
        self.delivery.deliver(msisdn, code, self.clock.iso(now))
        del code
        self._event(now, "otp_issued", msisdn, ip=client_ip, otp_id=otp_id,
                    send_stage=res.stage, replaced_previous=res.replaced)
        return Result(200, {"status": "sent", "expires_in": s.code_ttl,
                            "resend_after": s.limits().delay_after(res.stage)})

    # ----------------------------------------------------------------- verify
    def verify(self, payload: Any, client_ip: str) -> Result:
        now = self.clock.now()

        # 1. IP-лимит — до разбора формата, кода и блокировки номера.
        wait = self.store.ip_hit(client_ip, now)
        if wait is not None:
            self._event(now, "verify_ip_rate_limited", ip=client_ip)
            return _retry(429, "too_many_requests", wait)

        # 2. Формат. Не увеличивает счётчик неверных вводов.
        try:
            msisdn, code = parse_verify(payload)
        except ValidationError as exc:
            self._event(now, "verify_bad_request", ip=client_ip)
            return Result(400, {"error": "bad_request", "detail": str(exc)})

        # 3–5. Блокировка, наличие и срок действия кода.
        found = self.store.fetch(msisdn, now)
        if found.status == "locked":
            self._event(now, "verify_rejected_locked", msisdn, ip=client_ip)
            return _retry(423, "locked", found.wait)
        if found.status == "none":
            self._event(now, "verify_failed", msisdn, ip=client_ip, reason="no_active_code")
            return Result(401, {"error": "invalid_code"})
        if found.status == "expired":
            self._event(now, "verify_failed", msisdn, ip=client_ip, reason="expired",
                        otp_id=found.otp_id)
            return Result(410, {"error": "code_expired"})

        # 6. Сравнение HMAC за постоянное время, затем атомарный CAS в хранилище.
        if self.hasher.matches(msisdn, found.otp_id, code, found.code_hash):
            out = self.store.consume(msisdn, found.otp_id, now)
            if out.status == "ok":
                self._event(now, "verify_success", msisdn, ip=client_ip, otp_id=found.otp_id)
                return Result(200, {"status": "verified"})
            if out.status == "locked":
                self._event(now, "verify_rejected_locked", msisdn, ip=client_ip)
                return _retry(423, "locked", out.wait)
            # параллельный запрос уже погасил этот код
            self._event(now, "verify_failed", msisdn, ip=client_ip, reason="already_used",
                        otp_id=found.otp_id)
            return Result(401, {"error": "invalid_code"})

        out = self.store.fail(msisdn, found.otp_id, now)
        if out.status == "wrong":
            self._event(now, "verify_failed", msisdn, ip=client_ip, reason="wrong_code",
                        otp_id=found.otp_id, failures=out.failures)
            return Result(401, {"error": "invalid_code",
                                "attempts_left": self.settings.max_failures - out.failures})
        if out.status == "locked_now":
            self._event(now, "verify_failed", msisdn, ip=client_ip, reason="wrong_code",
                        otp_id=found.otp_id, failures=out.failures)
            self._event(now, "number_locked", msisdn, ip=client_ip,
                        locked_for=self.settings.lock_duration, failures=out.failures)
            return _retry(423, "locked", out.wait)
        if out.status == "locked":
            self._event(now, "verify_rejected_locked", msisdn, ip=client_ip)
            return _retry(423, "locked", out.wait)
        self._event(now, "verify_failed", msisdn, ip=client_ip, reason="no_active_code")
        return Result(401, {"error": "invalid_code"})
