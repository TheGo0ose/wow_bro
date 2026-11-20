from __future__ import annotations

import hashlib
import hmac
import re
import secrets

MSISDN_RE = re.compile(r"^\d{7,15}$")
CODE_RE = re.compile(r"^\d{6}$")


def generate_otp_code() -> str:
    """Generate a cryptographically secure 6-digit OTP code."""
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_otp(secret: str, msisdn: str, code: str) -> str:
    """Compute HMAC-SHA256 hex digest for an MSISDN and OTP code pair."""
    return hmac.new(
        secret.encode("utf-8"),
        f"{msisdn}:{code}".encode(),
        hashlib.sha256,
    ).hexdigest()
