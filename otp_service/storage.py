"""Хранилища состояния: в памяти процесса и в Redis.

Интерфейс одинаковый. Каждая операция атомарна сама по себе:
  * MemoryStore — под одной threading.Lock;
  * RedisStore  — один Lua-скрипт на операцию (Redis выполняет его целиком,
    ни одна другая команда не вклинится посередине), поэтому корректно и при
    нескольких процессах/репликах сервиса.

Проверка кода идёт в три шага: fetch -> сравнение HMAC в Python
(hmac.compare_digest) -> consume или fail. consume/fail — это compare-and-set
по otp_id: из N параллельных верных запросов погасить код сможет ровно один,
остальные увидят, что кода уже нет.

Время всегда передаётся из Python (часы сервиса), а не берётся из Redis —
поэтому подменяемые часы тестового режима работают и с Redis. TTL ключей в
Redis — это только «сборка мусора»: он всегда длиннее логического срока.
"""

from __future__ import annotations

import math
import secrets
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Protocol


# ------------------------------------------------------------------ результаты
@dataclass
class IssueResult:
    status: str                    # ok | locked | window_full | delay
    stage: int = 0                 # порядковый номер выдачи в окне (для ok)
    replaced: bool = False         # был ли прежний активный код
    wait: float = 0.0              # сколько ждать (для отказов)
    delay: float = 0.0             # требуемая задержка (для delay)
    sends_in_window: int = 0


@dataclass
class FetchResult:
    status: str                    # ok | locked | none | expired
    otp_id: str = ""
    code_hash: bytes = b""
    wait: float = 0.0


@dataclass
class OutcomeResult:
    status: str                    # ok | wrong | locked_now | locked | none
    failures: int = 0
    wait: float = 0.0


@dataclass
class Limits:
    code_ttl: float
    send_window: float
    send_max: int
    send_delays: tuple[float, ...]
    max_failures: int
    lock_duration: float
    ip_window: float
    ip_max: int

    def delay_after(self, stage: int) -> float:
        return self.send_delays[min(stage, len(self.send_delays)) - 1]


class Store(Protocol):
    name: str

    def ip_hit(self, ip: str, now: float) -> float | None:
        """Учесть verify с IP. None — разрешено, иначе — сколько ждать."""

    def issue(self, msisdn: str, now: float, otp_id: str, code_hash: bytes) -> IssueResult: ...
    def fetch(self, msisdn: str, now: float) -> FetchResult: ...
    def consume(self, msisdn: str, otp_id: str, now: float) -> OutcomeResult: ...
    def fail(self, msisdn: str, otp_id: str, now: float) -> OutcomeResult: ...
    def reset(self) -> None: ...


# ===================================================================== memory
@dataclass
class _Otp:
    otp_id: str
    code_hash: bytes
    issued_at: float
    failures: int = 0


@dataclass
class _Number:
    otp: _Otp | None = None
    sends: deque[tuple[float, int]] = field(default_factory=deque)   # (ts, stage)
    locked_until: float | None = None


class MemoryStore:
    name = "memory"

    def __init__(self, limits: Limits) -> None:
        self.limits = limits
        self._lock = threading.Lock()
        self._numbers: dict[str, _Number] = {}
        self._ip_hits: dict[str, deque[float]] = {}

    def reset(self) -> None:
        with self._lock:
            self._numbers.clear()
            self._ip_hits.clear()

    def _num(self, msisdn: str) -> _Number:
        return self._numbers.setdefault(msisdn, _Number())

    @staticmethod
    def _lock_left(st: _Number, now: float) -> float | None:
        if st.locked_until is None:
            return None
        if now < st.locked_until:                     # блокировка на [t, t+900)
            return st.locked_until - now
        st.locked_until = None
        return None

    def ip_hit(self, ip: str, now: float) -> float | None:
        lim = self.limits
        with self._lock:
            hits = self._ip_hits.setdefault(ip, deque())
            window_start = now - lim.ip_window        # окно (t-60, t]
            while hits and hits[0] <= window_start:
                hits.popleft()
            if len(hits) >= lim.ip_max:
                return hits[0] + lim.ip_window - now
            hits.append(now)
            return None

    def issue(self, msisdn: str, now: float, otp_id: str, code_hash: bytes) -> IssueResult:
        lim = self.limits
        with self._lock:
            st = self._num(msisdn)
            left = self._lock_left(st, now)
            if left is not None:
                return IssueResult("locked", wait=left)
            window_start = now - lim.send_window      # окно (t-600, t]
            while st.sends and st.sends[0][0] <= window_start:
                st.sends.popleft()
            n = len(st.sends)
            if n >= lim.send_max:
                return IssueResult("window_full", wait=st.sends[0][0] + lim.send_window - now,
                                   sends_in_window=n)
            if n:
                last_ts, last_stage = st.sends[-1]
                delay = lim.delay_after(last_stage)
                if now - last_ts < delay:
                    return IssueResult("delay", wait=delay - (now - last_ts), delay=delay)
            replaced = st.otp is not None
            st.otp = _Otp(otp_id, code_hash, now)
            st.sends.append((now, n + 1))
            return IssueResult("ok", stage=n + 1, replaced=replaced)

    def fetch(self, msisdn: str, now: float) -> FetchResult:
        with self._lock:
            st = self._numbers.get(msisdn)
            if st is None:
                return FetchResult("none")
            left = self._lock_left(st, now)
            if left is not None:
                return FetchResult("locked", wait=left)
            if st.otp is None:
                return FetchResult("none")
            if now - st.otp.issued_at >= self.limits.code_ttl:
                return FetchResult("expired", otp_id=st.otp.otp_id)
            return FetchResult("ok", otp_id=st.otp.otp_id, code_hash=st.otp.code_hash)

    def consume(self, msisdn: str, otp_id: str, now: float) -> OutcomeResult:
        with self._lock:
            st = self._numbers.get(msisdn)
            if st is None:
                return OutcomeResult("none")
            if st.locked_until is not None and now < st.locked_until:
                return OutcomeResult("locked", wait=st.locked_until - now)
            if st.otp is None or st.otp.otp_id != otp_id:
                return OutcomeResult("none")
            st.otp = None                             # атомарное погашение
            return OutcomeResult("ok")

    def fail(self, msisdn: str, otp_id: str, now: float) -> OutcomeResult:
        lim = self.limits
        with self._lock:
            st = self._numbers.get(msisdn)
            if st is None:
                return OutcomeResult("none")
            if st.locked_until is not None and now < st.locked_until:
                return OutcomeResult("locked", wait=st.locked_until - now)
            if st.otp is None or st.otp.otp_id != otp_id:
                return OutcomeResult("none")
            st.otp.failures += 1
            if st.otp.failures >= lim.max_failures:
                failures = st.otp.failures
                st.otp = None                         # код после блокировки не действует
                st.locked_until = now + lim.lock_duration
                return OutcomeResult("locked_now", failures=failures, wait=lim.lock_duration)
            return OutcomeResult("wrong", failures=st.otp.failures)

    # для тестов: убедиться, что открытый код не хранится
    def dump(self) -> str:
        with self._lock:
            return repr(self._numbers)


# ====================================================================== redis
_LUA_LOCK_CHECK = """
local function lock_left(key, now)
  local v = redis.call('GET', key)
  if not v then return nil end
  if now < tonumber(v) then return v end
  redis.call('DEL', key)
  return nil
end
"""

# KEYS: otp, sends, lock
# ARGV: now, window_start, send_max, otp_id, hash_hex, otp_ttl, sends_ttl, delay1, delay2, ...
_LUA_ISSUE = _LUA_LOCK_CHECK + """
local now = tonumber(ARGV[1])
local locked = lock_left(KEYS[3], now)
if locked then return {'locked', locked} end
redis.call('ZREMRANGEBYSCORE', KEYS[2], '-inf', ARGV[2])
local n = redis.call('ZCARD', KEYS[2])
if n >= tonumber(ARGV[3]) then
  local first = redis.call('ZRANGE', KEYS[2], 0, 0, 'WITHSCORES')
  return {'window_full', first[2], tostring(n)}
end
if n > 0 then
  local last = redis.call('ZRANGE', KEYS[2], -1, -1, 'WITHSCORES')
  local stage = tonumber(string.match(last[1], '^(%d+)|'))
  local ndelays = #ARGV - 7
  local delay = ARGV[7 + math.min(stage, ndelays)]
  if now - tonumber(last[2]) < tonumber(delay) then
    return {'delay', last[2], delay}
  end
end
local replaced = redis.call('EXISTS', KEYS[1])
redis.call('DEL', KEYS[1])
redis.call('HSET', KEYS[1], 'id', ARGV[4], 'hash', ARGV[5], 'issued', ARGV[1], 'failures', 0)
redis.call('EXPIRE', KEYS[1], ARGV[6])
redis.call('ZADD', KEYS[2], ARGV[1], tostring(n + 1) .. '|' .. ARGV[4])
redis.call('EXPIRE', KEYS[2], ARGV[7])
return {'ok', tostring(n + 1), tostring(replaced)}
"""

# KEYS: otp, lock    ARGV: now, code_ttl
_LUA_FETCH = _LUA_LOCK_CHECK + """
local now = tonumber(ARGV[1])
local locked = lock_left(KEYS[2], now)
if locked then return {'locked', locked} end
local otp = redis.call('HMGET', KEYS[1], 'id', 'hash', 'issued')
if not otp[1] then return {'none'} end
if now - tonumber(otp[3]) >= tonumber(ARGV[2]) then return {'expired', otp[1]} end
return {'ok', otp[1], otp[2]}
"""

# KEYS: otp, lock    ARGV: now, otp_id
_LUA_CONSUME = """
local lock = redis.call('GET', KEYS[2])
if lock and tonumber(ARGV[1]) < tonumber(lock) then return {'locked', lock} end
if redis.call('HGET', KEYS[1], 'id') ~= ARGV[2] then return {'none'} end
redis.call('DEL', KEYS[1])
return {'ok'}
"""

# KEYS: otp, lock    ARGV: now, otp_id, max_failures, locked_until, lock_ttl
_LUA_FAIL = """
local lock = redis.call('GET', KEYS[2])
if lock and tonumber(ARGV[1]) < tonumber(lock) then return {'locked', lock} end
if redis.call('HGET', KEYS[1], 'id') ~= ARGV[2] then return {'none'} end
local f = redis.call('HINCRBY', KEYS[1], 'failures', 1)
if f >= tonumber(ARGV[3]) then
  redis.call('DEL', KEYS[1])
  redis.call('SET', KEYS[2], ARGV[4], 'EX', ARGV[5])
  return {'locked_now', tostring(f)}
end
return {'wrong', tostring(f)}
"""

# KEYS: ip    ARGV: now, window_start, ip_max, token, ttl
_LUA_IP_HIT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[2])
local n = redis.call('ZCARD', KEYS[1])
if n >= tonumber(ARGV[3]) then
  local first = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  return {'limited', first[2]}
end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[4])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return {'ok'}
"""


def _num(x: float) -> str:
    """Число -> строка без потери точности (Lua tostring округляет до 14 знаков,
    поэтому все моменты времени вычисляются в Python и передаются строкой)."""
    return repr(float(x))


def _ttl(seconds: float) -> str:
    return str(max(1, math.ceil(seconds)))


class RedisStore:
    """Хранилище в Redis с TTL. Ключи одного номера в одном hash-slot ({msisdn}),
    так что скрипты работают и в Redis Cluster.

        {p}otp:{m}    HASH  id, hash(hex HMAC), issued, failures   TTL = срок кода + 1 ч
        {p}sends:{m}  ZSET  "stage|otp_id" -> ts                   TTL = окно 10 мин
        {p}lock:{m}   STR   locked_until                           TTL = 15 мин
        {p}ip:{ip}    ZSET  token -> ts                            TTL = окно 1 мин
    """

    name = "redis"
    OTP_RETENTION = 3600.0   # сколько хранить запись о коде после выдачи (для ответа 410)

    def __init__(self, client: Any, limits: Limits, prefix: str = "otp:") -> None:
        self.r = client
        self.limits = limits
        self.prefix = prefix
        self._issue = client.register_script(_LUA_ISSUE)
        self._fetch = client.register_script(_LUA_FETCH)
        self._consume = client.register_script(_LUA_CONSUME)
        self._fail = client.register_script(_LUA_FAIL)
        self._ip_hit = client.register_script(_LUA_IP_HIT)

    @classmethod
    def from_url(cls, url: str, limits: Limits, prefix: str = "otp:") -> "RedisStore":
        import redis

        client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=5)
        client.ping()   # падаем сразу и понятно, если Redis недоступен
        return cls(client, limits, prefix)

    def _keys(self, msisdn: str) -> tuple[str, str, str]:
        tag = "{" + msisdn + "}"
        p = self.prefix
        return f"{p}otp:{tag}", f"{p}sends:{tag}", f"{p}lock:{tag}"

    @staticmethod
    def _s(v: Any) -> str:
        return v.decode() if isinstance(v, bytes) else str(v)

    def reset(self) -> None:
        keys = list(self.r.scan_iter(match=self.prefix + "*", count=500))
        if keys:
            self.r.delete(*keys)

    def ip_hit(self, ip: str, now: float) -> float | None:
        lim = self.limits
        res = self._ip_hit(keys=[f"{self.prefix}ip:{ip}"],
                           args=[_num(now), _num(now - lim.ip_window), lim.ip_max,
                                 secrets.token_hex(8), _ttl(lim.ip_window + 60)])
        if self._s(res[0]) == "limited":
            return float(self._s(res[1])) + lim.ip_window - now
        return None

    def issue(self, msisdn: str, now: float, otp_id: str, code_hash: bytes) -> IssueResult:
        lim = self.limits
        otp_k, sends_k, lock_k = self._keys(msisdn)
        res = [self._s(x) for x in self._issue(
            keys=[otp_k, sends_k, lock_k],
            args=[_num(now), _num(now - lim.send_window), lim.send_max, otp_id, code_hash.hex(),
                  _ttl(lim.code_ttl + self.OTP_RETENTION), _ttl(lim.send_window + 60),
                  *[_num(d) for d in lim.send_delays]])]
        status = res[0]
        if status == "locked":
            return IssueResult("locked", wait=float(res[1]) - now)
        if status == "window_full":
            return IssueResult("window_full", wait=float(res[1]) + lim.send_window - now,
                               sends_in_window=int(res[2]))
        if status == "delay":
            delay = float(res[2])
            return IssueResult("delay", wait=delay - (now - float(res[1])), delay=delay)
        return IssueResult("ok", stage=int(res[1]), replaced=res[2] == "1")

    def fetch(self, msisdn: str, now: float) -> FetchResult:
        otp_k, _, lock_k = self._keys(msisdn)
        res = [self._s(x) for x in self._fetch(keys=[otp_k, lock_k],
                                                args=[_num(now), _num(self.limits.code_ttl)])]
        if res[0] == "locked":
            return FetchResult("locked", wait=float(res[1]) - now)
        if res[0] == "none":
            return FetchResult("none")
        if res[0] == "expired":
            return FetchResult("expired", otp_id=res[1])
        return FetchResult("ok", otp_id=res[1], code_hash=bytes.fromhex(res[2]))

    def consume(self, msisdn: str, otp_id: str, now: float) -> OutcomeResult:
        otp_k, _, lock_k = self._keys(msisdn)
        res = [self._s(x) for x in self._consume(keys=[otp_k, lock_k], args=[_num(now), otp_id])]
        if res[0] == "locked":
            return OutcomeResult("locked", wait=float(res[1]) - now)
        return OutcomeResult(res[0])

    def fail(self, msisdn: str, otp_id: str, now: float) -> OutcomeResult:
        lim = self.limits
        otp_k, _, lock_k = self._keys(msisdn)
        res = [self._s(x) for x in self._fail(
            keys=[otp_k, lock_k],
            args=[_num(now), otp_id, lim.max_failures, _num(now + lim.lock_duration),
                  _ttl(lim.lock_duration + 60)])]
        if res[0] == "locked":
            return OutcomeResult("locked", wait=float(res[1]) - now)
        if res[0] == "locked_now":
            return OutcomeResult("locked_now", failures=int(res[1]), wait=lim.lock_duration)
        if res[0] == "wrong":
            return OutcomeResult("wrong", failures=int(res[1]))
        return OutcomeResult("none")

    # для тестов: всё содержимое Redis одной строкой
    def dump(self) -> str:
        out = []
        for k in self.r.scan_iter(match=self.prefix + "*"):
            t = self._s(self.r.type(k))
            if t == "hash":
                out.append(f"{k}={self.r.hgetall(k)}")
            elif t == "zset":
                out.append(f"{k}={self.r.zrange(k, 0, -1, withscores=True)}")
            else:
                out.append(f"{k}={self.r.get(k)}")
        return "\n".join(out)
