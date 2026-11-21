from __future__ import annotations

import threading
from dataclasses import asdict, dataclass
from typing import Any

OPERATOR_META: dict[str, tuple[str, str]] = {
    "Beeline KG": ("437-01", "MSC-BEE-01"),
    "MEGA": ("437-05", "MSC-MEGA-02"),
    "O!": ("437-09", "MSC-NUR-03"),
    "KT Mobile": ("437-03", "MSC-KTM-04"),
}

_OPERATOR_ALIASES: dict[str, str] = {
    "beeline kg": "Beeline KG",
    "beeline": "Beeline KG",
    "sky mobile": "Beeline KG",
    "mega": "MEGA",
    "megacom": "MEGA",
    "alfa telecom": "MEGA",
    "o!": "O!",
    "o": "O!",
    "nur": "O!",
    "nur telecom": "O!",
    "kt mobile": "KT Mobile",
    "kt": "KT Mobile",
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

_lock = threading.RLock()
_mnp_overrides: dict[str, str] = {}
_absent_overrides: dict[str, bool] = {}


@dataclass(frozen=True)
class HlrLookupResult:
    """Result of an HLR / MNP routing lookup for a subscriber MSISDN."""

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


def normalize_operator(name: str) -> str:
    """Normalize an operator display name or alias to a canonical KG operator."""
    cleaned = name.strip()
    canonical = _OPERATOR_ALIASES.get(cleaned.lower())
    if canonical is None:
        raise ValueError(f"Unsupported operator: {name}")
    return canonical


def _clean_msisdn(msisdn: str) -> str:
    return str(msisdn).strip().lstrip("+")


def _home_operator_for_msisdn(msisdn: str) -> str:
    prefix6 = msisdn[:6]
    if len(prefix6) == 6 and prefix6.isdigit():
        if "996770" <= prefix6 <= "996779" or "996220" <= prefix6 <= "996227":
            return "Beeline KG"
        if "996550" <= prefix6 <= "996559" or "996990" <= prefix6 <= "996999":
            return "MEGA"
        if "996700" <= prefix6 <= "996709" or "996500" <= prefix6 <= "996509":
            return "O!"
    return "KT Mobile"


def lookup_hlr(msisdn: str) -> HlrLookupResult:
    """Resolve HLR/MNP operator routing and subscriber presence for an MSISDN."""
    clean = _clean_msisdn(msisdn)
    home_op = _home_operator_for_msisdn(clean)

    with _lock:
        target_op = _mnp_overrides.get(clean) or DEFAULT_MNP_PORTED.get(clean, home_op)
        in_network = _absent_overrides.get(clean, clean not in DEFAULT_ABSENT_NUMBERS)

    ported = target_op != home_op
    display_op = f"{target_op} [MNP]" if ported else target_op
    mcc_mnc, msc_gt = OPERATOR_META.get(target_op, OPERATOR_META["KT Mobile"])
    hlr_status = "HLR_OK" if in_network else "ABSENT_SUBSCRIBER"

    return HlrLookupResult(
        msisdn=clean,
        home_operator=home_op,
        operator=display_op,
        mcc_mnc=mcc_mnc,
        msc_gt=msc_gt,
        ported=ported,
        hlr_status=hlr_status,
        in_network=in_network,
    )


def apply_hlr_override(
    *,
    msisdn: str,
    operator: str | None = None,
    in_network: bool | None = None,
) -> HlrLookupResult:
    """Update MNP operator and/or network presence override for an MSISDN."""
    clean = _clean_msisdn(msisdn)
    if not clean:
        raise ValueError("msisdn must not be empty")

    normalized_op: str | None = None
    if operator is not None and operator.strip():
        normalized_op = normalize_operator(operator)

    with _lock:
        if normalized_op is not None:
            _mnp_overrides[clean] = normalized_op
        if in_network is not None:
            _absent_overrides[clean] = bool(in_network)
        return lookup_hlr(clean)


def seed_hlr_overrides(rows: list[tuple[str, str | None, bool | None]]) -> None:
    """Replace in-memory HLR overrides from persisted database rows."""
    with _lock:
        _mnp_overrides.clear()
        _absent_overrides.clear()
        for msisdn, target_operator, in_network in rows:
            clean = _clean_msisdn(msisdn)
            if not clean:
                continue
            if target_operator is not None and target_operator.strip():
                try:
                    _mnp_overrides[clean] = normalize_operator(target_operator)
                except ValueError:
                    pass
            if in_network is not None:
                _absent_overrides[clean] = bool(in_network)


def reset_hlr_memory() -> None:
    """Clear all runtime HLR/MNP overrides, restoring default routing tables."""
    with _lock:
        _mnp_overrides.clear()
        _absent_overrides.clear()


def get_hlr_summary() -> dict[str, Any]:
    """Return a thread-safe snapshot of active MNP ports and absent subscribers."""
    with _lock:
        ported = dict(DEFAULT_MNP_PORTED)
        ported.update(_mnp_overrides)

        absent = set(DEFAULT_ABSENT_NUMBERS)
        for msisdn, is_present in _absent_overrides.items():
            if is_present:
                absent.discard(msisdn)
            else:
                absent.add(msisdn)

        return {
            "ported_count": len(ported),
            "absent_count": len(absent),
            "ported": ported,
            "absent": sorted(absent),
            "mnp_overrides": dict(_mnp_overrides),
            "absent_overrides": dict(_absent_overrides),
        }
