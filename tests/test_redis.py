"""Специфика Redis: TTL ключей и несколько реплик сервиса над одним Redis."""

from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from conftest import START, fake_redis, make_settings

from otp_service.clock import FakeClock
from otp_service.delivery import MemoryDelivery
from otp_service.security_log import SecurityLog
from otp_service.service import OtpService
from otp_service.storage import RedisStore

N = "996770730000"


def _replicas(tmp_path, count=2):
    """Несколько независимых OtpService (как разные процессы) над общим Redis и часами."""
    client = fake_redis()
    clock = FakeClock(START)
    delivery = MemoryDelivery()
    services = []
    for i in range(count):
        settings = make_settings(tmp_path / f"r{i}")
        store = RedisStore(client, settings.limits(), settings.redis_prefix)
        services.append(OtpService(settings, clock, delivery, SecurityLog(None), store=store))
    return client, clock, delivery, services


def test_keys_have_ttl(tmp_path):
    client, _, delivery, (svc, _) = _replicas(tmp_path)
    assert svc.send({"msisdn": N}).status == 200
    for _ in range(5):
        svc.verify({"msisdn": N, "code": "000000" if delivery.last_code(N) != "000000" else "000001"},
                   "192.0.2.10")
    keys = sorted(client.scan_iter(match="otp:*"))
    assert {k.split(":")[1] for k in keys} == {"sends", "lock", "ip"}   # otp-запись удалена при блокировке
    for k in keys:
        assert client.ttl(k) > 0, f"у ключа {k} нет TTL"
    assert 900 <= client.ttl("otp:lock:{" + N + "}") <= 960


def test_otp_record_ttl(tmp_path):
    client, _, _, (svc, _) = _replicas(tmp_path)
    svc.send({"msisdn": N})
    ttl = client.ttl("otp:otp:{" + N + "}")
    assert 120 < ttl <= 120 + RedisStore.OTP_RETENTION


def test_lock_shared_between_replicas(tmp_path):
    _, _, delivery, (a, b) = _replicas(tmp_path)
    assert a.send({"msisdn": N}).status == 200
    code = delivery.last_code(N)
    wrong = f"{(int(code) + 1) % 1_000_000:06d}"
    statuses = [(a if i % 2 else b).verify({"msisdn": N, "code": wrong}, "192.0.2.10").status
                for i in range(5)]
    assert statuses == [401, 401, 401, 401, 423]
    assert b.send({"msisdn": N}).status == 423
    assert a.verify({"msisdn": N, "code": code}, "192.0.2.10").status == 423


def test_rate_limits_shared_between_replicas(tmp_path):
    _, _, _, (a, b) = _replicas(tmp_path)
    assert a.send({"msisdn": N}).status == 200
    assert b.send({"msisdn": N}).status == 429          # задержка 30 с видна другой реплике
    for i in range(20):
        (a if i % 2 else b).verify({"msisdn": "996770730001", "code": "123456"}, "192.0.2.10")
    assert a.verify({"msisdn": "996770730001", "code": "123456"}, "192.0.2.10").status == 429
    assert b.verify({"msisdn": "996770730001", "code": "123456"}, "192.0.2.10").status == 429


def test_parallel_verify_across_replicas_exactly_one(tmp_path):
    _, _, delivery, services = _replicas(tmp_path, count=4)
    services[0].send({"msisdn": N})
    code = delivery.last_code(N)
    barrier = threading.Barrier(40)

    def attempt(i):
        barrier.wait()
        return services[i % 4].verify({"msisdn": N, "code": code}, f"10.0.0.{i % 3}").status

    with ThreadPoolExecutor(max_workers=40) as pool:
        statuses = Counter(pool.map(attempt, range(40)))
    assert statuses == {200: 1, 401: 39}
