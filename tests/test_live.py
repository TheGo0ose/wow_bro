"""Проверки по настоящему HTTP (uvicorn), которые TestClient не ловит."""

from __future__ import annotations

import httpx


def test_x_forwarded_for_spoof_does_not_bypass_ip_limit(live_server):
    """Регрессия: uvicorn с proxy_headers=True подставлял IP из X-Forwarded-For."""
    with httpx.Client(base_url=live_server["base_url"]) as c:
        c.post("/__test__/reset").raise_for_status()
        statuses = [
            c.post("/otp/verify", json={"msisdn": "996770730000", "code": "123456"},
                   headers={"X-Forwarded-For": f"203.0.113.{i}"}).status_code
            for i in range(21)
        ]
    assert statuses[:20] == [401] * 20
    assert statuses[20] == 429
