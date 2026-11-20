"""HTTP-слой: определение IP, служебные методы, отсутствие утечки кода."""

from __future__ import annotations

from fastapi.testclient import TestClient

from conftest import make_settings

from otp_service.app import create_app

N = "996770730000"


def _client(tmp_path, **overrides) -> TestClient:
    return TestClient(create_app(make_settings(tmp_path, **overrides)))


def test_x_forwarded_for_is_ignored(tmp_path):
    client = _client(tmp_path)
    for i in range(20):
        r = client.post("/otp/verify", json={"msisdn": N, "code": "123456"},
                        headers={"X-Forwarded-For": f"10.0.0.{i}"})
        assert r.status_code == 401
    r = client.post("/otp/verify", json={"msisdn": N, "code": "123456"},
                    headers={"X-Forwarded-For": "10.9.9.9"})
    assert r.status_code == 429      # подмена заголовка не обходит IP-лимит


def test_trusted_header_only_when_configured(tmp_path):
    client = _client(tmp_path, trusted_ip_header="X-Test-Client-IP")
    for i in range(25):
        r = client.post("/otp/verify", json={"msisdn": N, "code": "123456"},
                        headers={"X-Test-Client-IP": f"10.0.0.{i}"})
        assert r.status_code == 401


def test_trusted_header_ignored_from_untrusted_peer(tmp_path):
    client = _client(tmp_path, trusted_ip_header="X-Test-Client-IP", trusted_proxies=frozenset())
    statuses = [client.post("/otp/verify", json={"msisdn": N, "code": "123456"},
                            headers={"X-Test-Client-IP": f"10.0.0.{i}"}).status_code
                for i in range(21)]
    assert statuses[-1] == 429


def test_test_endpoints_hidden_in_normal_mode(tmp_path):
    client = _client(tmp_path)
    assert client.post("/__test__/advance", json={"seconds": 10}).status_code == 404
    assert client.post("/__test__/reset").status_code == 404


def test_no_public_way_to_get_code(tmp_path):
    client = _client(tmp_path, test_mode=True)
    r = client.post("/otp/send", json={"msisdn": N})
    assert r.status_code == 200
    code = __import__("json").loads(
        (tmp_path / "outbox" / "sms.jsonl").read_text("utf-8").splitlines()[-1])["code"]
    assert code not in r.text
    for path in ("/security/events", "/dashboard", "/health"):
        assert code not in client.get(path).text
    assert code not in client.post("/__test__/advance", json={"seconds": 1}).text


def test_outbox_line_format(tmp_path):
    client = _client(tmp_path, test_mode=True)
    client.post("/otp/send", json={"msisdn": N})
    import json
    line = json.loads((tmp_path / "outbox" / "sms.jsonl").read_text("utf-8").splitlines()[-1])
    assert set(line) == {"ts", "msisdn", "code"}
    assert line["ts"] == "2026-11-20T12:00:00+06:00"
    assert line["msisdn"] == N and len(line["code"]) == 6


def test_invalid_json_is_400(tmp_path):
    client = _client(tmp_path)
    r = client.post("/otp/send", content=b"{oops", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    r = client.post("/otp/verify", content=b"\xff\xfe", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
