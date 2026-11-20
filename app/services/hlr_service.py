from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

HLR_MNP_KEY = "hlr:mnp"
HLR_ABSENT_KEY = "hlr:absent"

_OPERATOR_METADATA: dict[str, tuple[str, str]] = {
    "Beeline KG": ("437-01", "MSC-BEE-01"),
    "MEGA": ("437-05", "MSC-MEGA-02"),
    "O!": ("437-09", "MSC-NUR-03"),
    "KT Mobile": ("437-03", "MSC-KTM-04"),
}

_OPERATOR_ALIASES: dict[str, str] = {
    "beeline": "Beeline KG",
    "beeline kg": "Beeline KG",
    "mega": "MEGA",
    "megacom": "MEGA",
    "o!": "O!",
    "nur": "O!",
    "nur telecom": "O!",
    "kt": "KT Mobile",
    "kt mobile": "KT Mobile",
}

DEFAULT_MNP_PORTED: dict[str, str] = {
    "996770730003": "O!",
    "996770730007": "MEGA",
    "996770730013": "O!",
}

DEFAULT_ABSENT_NUMBERS: frozenset[str] = frozenset(
    {
        "996770730008",
        "996770730048",
        "996770000404",
    }
)

_overrides_lock = threading.Lock()
_mnp_overrides: dict[str, str] = {}
_absent_overrides: dict[str, bool] = {}


@dataclass(frozen=True)
class HlrLookupResult:
    """Result of an HLR / MNP routing lookup for a KG MSISDN."""

    msisdn: str
    home_operator: str
    operator: str
    mcc_mnc: str
    msc_gt: str
    ported: bool
    hlr_status: str
    in_network: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialize lookup result to a JSON-compatible dictionary."""
        return asdict(self)


def _normalize_msisdn(msisdn: str) -> str:
    return re.sub(r"\D", "", str(msisdn or ""))


def _normalize_operator(operator: str) -> str:
    cleaned = operator.strip()
    return _OPERATOR_ALIASES.get(cleaned.lower(), cleaned)


def _decode_redis_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value)


def _resolve_home_operator(msisdn: str) -> str:
    if len(msisdn) >= 6:
        prefix = msisdn[:6]
        if ("996770" <= prefix <= "996779") or ("996220" <= prefix <= "996227"):
            return "Beeline KG"
        if ("996550" <= prefix <= "996559") or ("996990" <= prefix <= "996999"):
            return "MEGA"
        if ("996700" <= prefix <= "996709") or ("996500" <= prefix <= "996509"):
            return "O!"
    return "KT Mobile"


def _build_result(
    msisdn: str,
    mnp_target: str | None,
    is_absent: bool,
) -> HlrLookupResult:
    home_operator = _resolve_home_operator(msisdn)
    target_operator = mnp_target if mnp_target else home_operator
    ported = target_operator != home_operator
    operator_label = f"{target_operator} [MNP]" if ported else target_operator
    mcc_mnc, msc_gt = _OPERATOR_METADATA.get(target_operator, ("437-03", "MSC-KTM-04"))
    in_network = not is_absent
    hlr_status = "HLR_OK" if in_network else "ABSENT_SUBSCRIBER"
    return HlrLookupResult(
        msisdn=msisdn,
        home_operator=home_operator,
        operator=operator_label,
        mcc_mnc=mcc_mnc,
        msc_gt=msc_gt,
        ported=ported,
        hlr_status=hlr_status,
        in_network=in_network,
    )


def _snapshot_local_overrides() -> tuple[dict[str, str], dict[str, bool]]:
    with _overrides_lock:
        return dict(_mnp_overrides), dict(_absent_overrides)


def lookup_hlr(msisdn: str) -> HlrLookupResult:
    """Perform synchronous HLR/MNP lookup using defaults and in-memory overrides."""
    norm = _normalize_msisdn(msisdn)
    mnp_overrides, absent_overrides = _snapshot_local_overrides()
    mnp_target = mnp_overrides.get(norm, DEFAULT_MNP_PORTED.get(norm))
    is_absent = absent_overrides.get(norm, norm in DEFAULT_ABSENT_NUMBERS)
    return _build_result(norm, mnp_target, is_absent)


async def lookup_hlr_async(redis_client: Redis | None, msisdn: str) -> HlrLookupResult:
    """Perform async HLR/MNP lookup checking shared Redis overrides if available."""
    norm = _normalize_msisdn(msisdn)
    mnp_overrides, absent_overrides = _snapshot_local_overrides()
    mnp_target = mnp_overrides.get(norm, DEFAULT_MNP_PORTED.get(norm))
    is_absent = absent_overrides.get(norm, norm in DEFAULT_ABSENT_NUMBERS)

    if redis_client is not None:
        try:
            raw_mnp = await redis_client.hget(HLR_MNP_KEY, norm)
            if raw_mnp is not None:
                mnp_target = _normalize_operator(_decode_redis_text(raw_mnp))
            raw_absent = await redis_client.hget(HLR_ABSENT_KEY, norm)
            if raw_absent is not None:
                is_absent = _decode_redis_text(raw_absent) == "1"
        except (RedisError, OSError):
            pass

    return _build_result(norm, mnp_target, is_absent)


def set_subscriber_presence(msisdn: str, in_network: bool) -> HlrLookupResult:
    """Synchronously set subscriber network presence in memory and return updated HLR info."""
    norm = _normalize_msisdn(msisdn)
    with _overrides_lock:
        _absent_overrides[norm] = not in_network
    return lookup_hlr(norm)


async def register_mnp(
    redis_client: Redis | None,
    *,
    msisdn: str,
    operator: str | None = None,
    in_network: bool | None = None,
) -> HlrLookupResult:
    """Register an MNP operator port and/or subscriber presence override."""
    norm = _normalize_msisdn(msisdn)
    normalized_op = _normalize_operator(operator) if operator is not None else None

    with _overrides_lock:
        if normalized_op is not None:
            _mnp_overrides[norm] = normalized_op
        if in_network is not None:
            _absent_overrides[norm] = not in_network

    if redis_client is not None:
        try:
            if normalized_op is not None:
                await redis_client.hset(HLR_MNP_KEY, norm, normalized_op)
            if in_network is not None:
                await redis_client.hset(HLR_ABSENT_KEY, norm, "0" if in_network else "1")
        except (RedisError, OSError):
            pass

    return await lookup_hlr_async(redis_client, norm)


def _compose_summary(
    mnp_map: dict[str, str],
    absent_flags: dict[str, bool],
) -> dict[str, Any]:
    ported: dict[str, str] = {}
    for num, op in mnp_map.items():
        if op != _resolve_home_operator(num):
            ported[num] = op

    absent = set(DEFAULT_ABSENT_NUMBERS)
    for num, flag in absent_flags.items():
        if flag:
            absent.add(num)
        else:
            absent.discard(num)

    absent_sorted = sorted(absent)
    return {
        "ported_numbers": ported,
        "absent_numbers": absent_sorted,
        "ported_count": len(ported),
        "absent_count": len(absent_sorted),
        "operators": {
            op: {"mcc_mnc": meta[0], "msc_gt": meta[1]} for op, meta in _OPERATOR_METADATA.items()
        },
    }


def get_hlr_summary() -> dict[str, Any]:
    """Return a summary of active HLR/MNP routing rules using in-memory overrides."""
    mnp_overrides, absent_overrides = _snapshot_local_overrides()
    merged_mnp = {**DEFAULT_MNP_PORTED, **mnp_overrides}
    return _compose_summary(merged_mnp, absent_overrides)


async def get_hlr_summary_async(redis_client: Redis | None) -> dict[str, Any]:
    """Return a summary of active HLR/MNP routing rules including Redis overrides."""
    mnp_overrides, absent_overrides = _snapshot_local_overrides()
    merged_mnp = {**DEFAULT_MNP_PORTED, **mnp_overrides}
    merged_absent = dict(absent_overrides)

    if redis_client is not None:
        try:
            raw_mnp = await redis_client.hgetall(HLR_MNP_KEY)
            for k, v in raw_mnp.items():
                merged_mnp[_normalize_msisdn(_decode_redis_text(k))] = _normalize_operator(
                    _decode_redis_text(v)
                )
            raw_absent = await redis_client.hgetall(HLR_ABSENT_KEY)
            for k, v in raw_absent.items():
                merged_absent[_normalize_msisdn(_decode_redis_text(k))] = (
                    _decode_redis_text(v) == "1"
                )
        except (RedisError, OSError):
            pass

    return _compose_summary(merged_mnp, merged_absent)


async def reset_hlr_overrides(redis_client: Redis | None = None) -> None:
    """Clear all in-memory and Redis HLR/MNP overrides."""
    with _overrides_lock:
        _mnp_overrides.clear()
        _absent_overrides.clear()

    if redis_client is not None:
        try:
            await redis_client.delete(HLR_MNP_KEY, HLR_ABSENT_KEY)
        except (RedisError, OSError):
            pass
