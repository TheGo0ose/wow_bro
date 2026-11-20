from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from app.config import resolve_outbox_path
from app.core.clock import clock


def write_outbox_sms(msisdn: str, code: str, ts: str | None = None) -> Path:
    """Append a single OTP delivery record to the outbox JSONL file safely."""
    outbox_path = resolve_outbox_path()
    outbox_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": ts or clock.now_iso(),
        "msisdn": msisdn,
        "code": code,
    }
    line = json.dumps(record, ensure_ascii=False) + "\n"

    with open(outbox_path, "a", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    return outbox_path
