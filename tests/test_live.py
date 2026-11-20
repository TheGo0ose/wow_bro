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


def test_second_loopback_address_is_separate_ip(live_server):
    """Жюри подключается с 127.0.0.1 и 127.0.0.2 — у каждого адреса свой IP-лимит."""
    body = {"msisdn": "996770730000", "code": "123456"}
    with httpx.Client(base_url=live_server["base_url"]) as c:
        c.post("/__test__/reset").raise_for_status()
    clients = {src: httpx.Client(base_url=live_server["base_url"],
                                 transport=httpx.HTTPTransport(local_address=src))
               for src in ("127.0.0.1", "127.0.0.2")}
    try:
        first = [clients["127.0.0.1"].post("/otp/verify", json=body).status_code for _ in range(21)]
        second = clients["127.0.0.2"].post("/otp/verify", json=body).status_code
    finally:
        for c in clients.values():
            c.close()
    assert first[20] == 429
    assert second == 401
