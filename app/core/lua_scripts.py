from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING

from redis.asyncio import Redis

from app.config import get_settings

if TYPE_CHECKING:
    from app.config import Settings


@dataclass(frozen=True)
class SendLuaResult:
    """Result returned by the atomic SEND_OTP_LUA script."""

    status_code: int
    event: str
    retry_after: int


@dataclass(frozen=True)
class VerifyLuaResult:
    """Result returned by the atomic VERIFY_OTP_LUA script."""

    status_code: int
    event: str
    value: int


SEND_OTP_LUA = """
local lock_key = KEYS[1]
local send_hist_key = KEYS[2]
local otp_key = KEYS[3]
local fail_key = KEYS[4]

local now_ms = tonumber(ARGV[1])
local otp_hash = ARGV[2]
local otp_ttl_ms = tonumber(ARGV[3])
local key_ttl_sec = tonumber(ARGV[4])
local send_window_ms = tonumber(ARGV[5])
local send_limit = tonumber(ARGV[6])
local c1_ms = tonumber(ARGV[7])
local c2_ms = tonumber(ARGV[8])
local c3_ms = tonumber(ARGV[9])
local req_id = ARGV[10]

local locked_until_raw = redis.call('GET', lock_key)
if locked_until_raw then
    local locked_until = tonumber(locked_until_raw)
    if locked_until and now_ms < locked_until then
        local retry_sec = math.max(1, math.ceil((locked_until - now_ms) / 1000))
        return {423, 'MSISDN_LOCKED', retry_sec}
    else
        redis.call('DEL', lock_key)
        redis.call('DEL', fail_key)
    end
end

redis.call('ZREMRANGEBYSCORE', send_hist_key, '-inf', now_ms - send_window_ms)

local entries = redis.call('ZRANGE', send_hist_key, 0, -1, 'WITHSCORES')
local count = #entries / 2
if count >= send_limit then
    local oldest_ms = tonumber(entries[2])
    local retry_sec = math.max(1, math.ceil(((oldest_ms + send_window_ms) - now_ms) / 1000))
    return {429, 'RATE_LIMITED_SEND_10M', retry_sec}
end

local send_seq = tonumber(redis.call('HGET', otp_key, 'send_seq')) or 0
local last_sent_ms = tonumber(redis.call('HGET', otp_key, 'last_sent_ms'))
if last_sent_ms and (now_ms - last_sent_ms >= send_window_ms) then
    send_seq = 0
end

if send_seq > 0 and last_sent_ms then
    local cooldown_ms = c3_ms
    if send_seq == 1 then
        cooldown_ms = c1_ms
    elseif send_seq == 2 then
        cooldown_ms = c2_ms
    end
    local elapsed_ms = now_ms - last_sent_ms
    if elapsed_ms < cooldown_ms then
        local retry_sec = math.max(1, math.ceil((cooldown_ms - elapsed_ms) / 1000))
        return {429, 'RATE_LIMITED_COOLDOWN', retry_sec}
    end
end

local now_str = string.format('%.0f', now_ms)
local expires_str = string.format('%.0f', now_ms + otp_ttl_ms)
local next_seq = send_seq + 1
local otp_key_ttl = math.max(key_ttl_sec, math.ceil(send_window_ms / 1000))
redis.call('ZADD', send_hist_key, now_ms, now_str .. ':' .. req_id)
redis.call('EXPIRE', send_hist_key, key_ttl_sec)
redis.call(
    'HSET',
    otp_key,
    'hash', otp_hash,
    'expires_at_ms', expires_str,
    'status', 'active',
    'created_at_ms', now_str,
    'send_seq', tostring(next_seq),
    'last_sent_ms', now_str
)
redis.call('EXPIRE', otp_key, otp_key_ttl)

return {200, 'OTP_SENT', 0}
"""


VERIFY_OTP_LUA = """
local ip_rl_key = KEYS[1]
local lock_key = KEYS[2]
local otp_key = KEYS[3]
local fail_key = KEYS[4]

local now_ms = tonumber(ARGV[1])
local candidate_hash = ARGV[2]
local ip_window_ms = tonumber(ARGV[3])
local ip_limit = tonumber(ARGV[4])
local max_attempts = tonumber(ARGV[5])
local lockout_ttl_ms = tonumber(ARGV[6])
local key_ttl_sec = tonumber(ARGV[7])
local req_id = ARGV[8]

redis.call('ZREMRANGEBYSCORE', ip_rl_key, '-inf', now_ms - ip_window_ms)

local ip_entries = redis.call('ZRANGE', ip_rl_key, 0, -1, 'WITHSCORES')
local ip_count = #ip_entries / 2
if ip_count >= ip_limit then
    local oldest_ip_ms = tonumber(ip_entries[2])
    local retry_sec = math.max(1, math.ceil(((oldest_ip_ms + ip_window_ms) - now_ms) / 1000))
    return {429, 'RATE_LIMITED_IP', retry_sec}
end

local now_str = string.format('%.0f', now_ms)
redis.call('ZADD', ip_rl_key, now_ms, now_str .. ':' .. req_id)
redis.call('EXPIRE', ip_rl_key, key_ttl_sec)

local locked_until_raw = redis.call('GET', lock_key)
if locked_until_raw then
    local locked_until = tonumber(locked_until_raw)
    if locked_until and now_ms < locked_until then
        local retry_sec = math.max(1, math.ceil((locked_until - now_ms) / 1000))
        return {423, 'MSISDN_LOCKED', retry_sec}
    else
        redis.call('DEL', lock_key)
        redis.call('DEL', fail_key)
    end
end

local status = redis.call('HGET', otp_key, 'status')
if (not status) or status == 'used' then
    return {401, 'OTP_INVALID_OR_USED', 0}
end

local expires_at_ms = tonumber(redis.call('HGET', otp_key, 'expires_at_ms')) or 0
if now_ms >= expires_at_ms then
    return {410, 'OTP_EXPIRED', 0}
end

local stored_hash = redis.call('HGET', otp_key, 'hash') or ''
local s_len = #stored_hash
local c_len = #candidate_hash
local max_len = s_len
if c_len > max_len then
    max_len = c_len
end

local diff = bit.bxor(s_len, c_len)
for i = 1, max_len do
    local b1 = 0
    if i <= s_len then
        b1 = string.byte(stored_hash, i)
    end
    local b2 = 0
    if i <= c_len then
        b2 = string.byte(candidate_hash, i)
    end
    diff = bit.bor(diff, bit.bxor(b1, b2))
end

if diff == 0 then
    redis.call('HSET', otp_key, 'status', 'used')
    redis.call('DEL', fail_key)
    return {200, 'OTP_VERIFIED', 0}
end

local fails = redis.call('INCR', fail_key)
redis.call('EXPIRE', fail_key, key_ttl_sec)
if fails >= max_attempts then
    local lock_until_str = string.format('%.0f', now_ms + lockout_ttl_ms)
    redis.call('SET', lock_key, lock_until_str, 'EX', key_ttl_sec)
    redis.call('DEL', fail_key)
    return {423, 'MSISDN_LOCKED', math.max(1, math.ceil(lockout_ttl_ms / 1000))}
end

return {401, 'OTP_FAILED', max_attempts - fails}
"""


async def execute_send_otp(
    redis_client: Redis,
    *,
    msisdn: str,
    now_ms: int,
    otp_hash: str,
    settings: Settings | None = None,
    req_id: str | None = None,
) -> SendLuaResult:
    """Execute the atomic OTP send Lua script in Redis."""
    cfg = settings or get_settings()
    resolved_req_id = req_id or uuid.uuid4().hex
    cooldowns = cfg.COOLDOWNS or [30, 60, 120]
    c1_sec = cooldowns[0] if len(cooldowns) > 0 else 30
    c2_sec = cooldowns[1] if len(cooldowns) > 1 else c1_sec
    c3_sec = cooldowns[2] if len(cooldowns) > 2 else c2_sec

    keys = [
        f"lock:{msisdn}",
        f"send_hist:{msisdn}",
        f"otp:{msisdn}",
        f"fail:{msisdn}",
    ]
    args = [
        str(now_ms),
        otp_hash,
        str(cfg.OTP_TTL * 1000),
        str(cfg.OTP_KEY_TTL),
        str(cfg.SEND_WINDOW_SECONDS * 1000),
        str(cfg.SEND_LIMIT_10M),
        str(c1_sec * 1000),
        str(c2_sec * 1000),
        str(c3_sec * 1000),
        resolved_req_id,
    ]
    script = redis_client.register_script(SEND_OTP_LUA)
    raw = await script(keys=keys, args=args)
    return SendLuaResult(
        status_code=int(raw[0]),
        event=str(raw[1]),
        retry_after=int(raw[2]),
    )


async def execute_verify_otp(
    redis_client: Redis,
    *,
    msisdn: str,
    client_ip: str,
    now_ms: int,
    candidate_hash: str,
    settings: Settings | None = None,
    req_id: str | None = None,
) -> VerifyLuaResult:
    """Execute the atomic OTP verify Lua script in Redis."""
    cfg = settings or get_settings()
    resolved_req_id = req_id or uuid.uuid4().hex

    keys = [
        f"ip_rl:{client_ip}",
        f"lock:{msisdn}",
        f"otp:{msisdn}",
        f"fail:{msisdn}",
    ]
    args = [
        str(now_ms),
        candidate_hash,
        str(cfg.IP_WINDOW_SECONDS * 1000),
        str(cfg.IP_LIMIT_PER_MIN),
        str(cfg.MAX_VERIFY_ATTEMPTS),
        str(cfg.LOCKOUT_TTL * 1000),
        str(cfg.OTP_KEY_TTL),
        resolved_req_id,
    ]
    script = redis_client.register_script(VERIFY_OTP_LUA)
    raw = await script(keys=keys, args=args)
    return VerifyLuaResult(
        status_code=int(raw[0]),
        event=str(raw[1]),
        value=int(raw[2]),
    )
