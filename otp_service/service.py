"""Бизнес-логика OTP. Не зависит от HTTP — тестируется напрямую.

Атомарность: всё состояние защищено одним threading.Lock. Любая операция
send/verify целиком (проверки + изменение состояния) выполняется под ним,
поэтому из N параллельных verify с верным кодом успешен ровно один.
"""

from __future__ import annotations

import math
import secrets
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .clock import Clock
from .codes import CodeHasher, CodeSource, secure_code
from .config import Settings
from .delivery import Delivery
from .security_log import SecurityLog
from .validation import ValidationError, parse_msisdn, parse_verify


@dataclass
class Result:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class ActiveOtp:
    otp_id: str
    code_hash: bytes          # только HMAC, открытый код не хранится
    issued_at: float
    failures: int = 0         # неверные вводы, относящиеся к этому OTP


@dataclass
class SendRecord:
    ts: float
    stage: int                # порядковый номер выдачи в окне: 1, 2, 3


@dataclass
class NumberState:
    otp: ActiveOtp | None = None
    sends: deque[SendRecord] = field(default_factory=deque)
    locked_until: float | None = None


def mask_msisdn(msisdn: str) -> str:
    """996770730000 -> 996770***000 (для журнала и дашборда)."""
    return msisdn[:6] + "***" + msisdn[-3:] if len(msisdn) == 12 else "***"


def _retry_after(seconds: float) -> dict[str, str]:
    return {"Retry-After": str(max(1, math.ceil(seconds)))}


class OtpService:
    def __init__(
        self,
        settings: Settings,
        clock: Clock,
        delivery: Delivery,
        security_log: SecurityLog,
        code_source: CodeSource = secure_code,
        avoid_repeat_code: bool = False,
    ) -> None:
        self.settings = settings
        self.clock = clock
        self.delivery = delivery
        self.log = security_log
        self.code_source = code_source
        # В тестовом режиме новый код гарантированно отличается от предыдущего,
        # чтобы сценарии «новый send аннулирует старый код» были детерминированы.
        self.avoid_repeat_code = avoid_repeat_code
        self.hasher = CodeHasher(settings.secret)
        self._lock = threading.Lock()
        self._numbers: dict[str, NumberState] = {}
        self._ip_hits: dict[str, deque[float]] = {}
        self._last_code_hash: dict[str, bytes] = {}

    # ------------------------------------------------------------------ utils
    def reset(self) -> None:
        with self._lock:
            self._numbers.clear()
            self._ip_hits.clear()
            self._last_code_hash.clear()

    def _state(self, msisdn: str) -> NumberState:
        st = self._numbers.get(msisdn)
        if st is None:
            st = self._numbers[msisdn] = NumberState()
        return st

    def _event(self, now: float, event: str, msisdn: str | None = None, **fields: Any) -> None:
        if msisdn is not None:
            fields["msisdn"] = mask_msisdn(msisdn)
        self.log.write(self.clock.iso(now), event, **fields)

    def _refresh_lock(self, st: NumberState, now: float) -> float | None:
        """Возвращает оставшееся время блокировки или None, если блокировки нет."""
        if st.locked_until is None:
            return None
        if now < st.locked_until:          # блокировка действует на [t, t+900)
            return st.locked_until - now
        st.locked_until = None
        return None

    def _prune_sends(self, st: NumberState, now: float) -> None:
        # Окно (t-600, t]: выдача ровно в t-600 уже вне окна.
        window_start = now - self.settings.send_window
        while st.sends and st.sends[0].ts <= window_start:
            st.sends.popleft()

    def _new_code(self, msisdn: str, otp_id: str) -> tuple[str, bytes]:
        code = self.code_source()
        if self.avoid_repeat_code:
            prev = self._last_code_hash.get(msisdn)
            for _ in range(10):
                if prev is None or not self.hasher.matches(msisdn, "prev", code, prev):
                    break
                code = self.code_source()
            self._last_code_hash[msisdn] = self.hasher.digest(msisdn, "prev", code)
        return code, self.hasher.digest(msisdn, otp_id, code)

    # ------------------------------------------------------------------- send
    def send(self, payload: Any, client_ip: str | None = None) -> Result:
        try:
            msisdn = parse_msisdn(payload)
        except ValidationError as exc:
            return Result(400, {"error": "bad_request", "detail": str(exc)})

        s = self.settings
        with self._lock:
            now = self.clock.now()
            st = self._state(msisdn)

            lock_left = self._refresh_lock(st, now)
            if lock_left is not None:
                self._event(now, "send_rejected_locked", msisdn, ip=client_ip)
                return Result(423, {"error": "locked", "retry_after": math.ceil(lock_left)},
                              _retry_after(lock_left))

            self._prune_sends(st, now)
            if len(st.sends) >= s.send_max_in_window:
                wait = st.sends[0].ts + s.send_window - now
                self._event(now, "send_rate_limited", msisdn, ip=client_ip,
                            reason="window_full", sends_in_window=len(st.sends))
                return Result(429, {"error": "too_many_requests", "retry_after": math.ceil(wait)},
                              _retry_after(wait))

            if st.sends:
                last = st.sends[-1]
                delay = s.send_delays[min(last.stage, len(s.send_delays)) - 1]
                elapsed = now - last.ts
                if elapsed < delay:
                    wait = delay - elapsed
                    self._event(now, "send_rate_limited", msisdn, ip=client_ip,
                                reason="resend_delay", required_delay=delay)
                    return Result(429, {"error": "too_many_requests", "retry_after": math.ceil(wait)},
                                  _retry_after(wait))

            otp_id = secrets.token_hex(8)
            code, code_hash = self._new_code(msisdn, otp_id)
            replaced = st.otp is not None
            st.otp = ActiveOtp(otp_id=otp_id, code_hash=code_hash, issued_at=now)
            stage = len(st.sends) + 1
            st.sends.append(SendRecord(ts=now, stage=stage))

            # Доставка внутри критической секции: порядок строк в outbox
            # совпадает с порядком выдач, «последняя строка» = действующий код.
            self.delivery.deliver(msisdn, code, self.clock.iso(now))
            del code
            self._event(now, "otp_issued", msisdn, ip=client_ip, otp_id=otp_id,
                        send_stage=stage, replaced_previous=replaced)

        next_delay = s.send_delays[min(stage, len(s.send_delays)) - 1]
        return Result(200, {"status": "sent", "expires_in": s.code_ttl,
                            "resend_after": next_delay})

    # ----------------------------------------------------------------- verify
    def _ip_allowed(self, ip: str, now: float) -> tuple[bool, float]:
        s = self.settings
        hits = self._ip_hits.setdefault(ip, deque())
        window_start = now - s.ip_window      # окно (t-60, t]
        while hits and hits[0] <= window_start:
            hits.popleft()
        if len(hits) >= s.ip_max_in_window:
            return False, hits[0] + s.ip_window - now
        hits.append(now)
        return True, 0.0

    def verify(self, payload: Any, client_ip: str) -> Result:
        s = self.settings
        with self._lock:
            now = self.clock.now()

            # 1. IP-лимит — до разбора формата, кода и блокировки номера.
            allowed, wait = self._ip_allowed(client_ip, now)
            if not allowed:
                self._event(now, "verify_ip_rate_limited", ip=client_ip)
                return Result(429, {"error": "too_many_requests", "retry_after": math.ceil(wait)},
                              _retry_after(wait))

            # 2. Формат. Не увеличивает счётчик неверных вводов.
            try:
                msisdn, code = parse_verify(payload)
            except ValidationError as exc:
                self._event(now, "verify_bad_request", ip=client_ip)
                return Result(400, {"error": "bad_request", "detail": str(exc)})

            st = self._numbers.get(msisdn)

            # 3. Блокировка номера — даже верный код даёт 423.
            if st is not None:
                lock_left = self._refresh_lock(st, now)
                if lock_left is not None:
                    self._event(now, "verify_rejected_locked", msisdn, ip=client_ip)
                    return Result(423, {"error": "locked", "retry_after": math.ceil(lock_left)},
                                  _retry_after(lock_left))

            # 4. Нет активного кода (не выдавался / уже погашен / аннулирован).
            otp = st.otp if st is not None else None
            if otp is None:
                self._event(now, "verify_failed", msisdn, ip=client_ip, reason="no_active_code")
                return Result(401, {"error": "invalid_code"})

            # 5. Истёк: elapsed >= 120 c. Счётчик не растёт.
            if now - otp.issued_at >= s.code_ttl:
                self._event(now, "verify_failed", msisdn, ip=client_ip, reason="expired",
                            otp_id=otp.otp_id)
                return Result(410, {"error": "code_expired"})

            # 6. Сравнение хешей за постоянное время.
            if self.hasher.matches(msisdn, otp.otp_id, code, otp.code_hash):
                st.otp = None                  # атомарное погашение
                self._event(now, "verify_success", msisdn, ip=client_ip, otp_id=otp.otp_id)
                return Result(200, {"status": "verified"})

            otp.failures += 1
            if otp.failures >= s.max_failures:
                st.locked_until = now + s.lock_duration
                st.otp = None                  # код после блокировки больше не действует
                self._event(now, "verify_failed", msisdn, ip=client_ip, reason="wrong_code",
                            otp_id=otp.otp_id, failures=otp.failures)
                self._event(now, "number_locked", msisdn, ip=client_ip,
                            locked_for=s.lock_duration, failures=otp.failures)
                return Result(423, {"error": "locked", "retry_after": math.ceil(s.lock_duration)},
                              _retry_after(s.lock_duration))

            self._event(now, "verify_failed", msisdn, ip=client_ip, reason="wrong_code",
                        otp_id=otp.otp_id, failures=otp.failures)
            return Result(401, {"error": "invalid_code",
                                "attempts_left": s.max_failures - otp.failures})
