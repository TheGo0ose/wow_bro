"""Unit-тесты бизнес-логики с подменёнными часами."""

from __future__ import annotations

import json
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from conftest import Env

from otp_service.codes import CodeHasher, secure_code
from otp_service.validation import ValidationError, parse_verify

N = "996770730000"


# ---------------------------------------------------------------- генерация
def test_secure_code_format():
    for _ in range(2000):
        assert re.fullmatch(r"\d{6}", secure_code())


def test_leading_zeros_and_000000_accepted(tmp_path):
    env = Env(tmp_path, codes=["000000", "000123"])
    assert env.send() == 200
    assert env.code() == "000000"
    assert env.verify("000000") == 200
    env.clock.advance(30)
    assert env.send() == 200
    assert env.verify("123") == 400          # без ведущих нулей — это ошибка формата
    assert env.verify("000123") == 200


def test_unicode_digits_rejected():
    try:
        parse_verify({"msisdn": N, "code": "١٢٣٤٥٦"})  # арабские цифры
    except ValidationError:
        return
    raise AssertionError("юникод-цифры должны отклоняться")


# ---------------------------------------------------------------- хранение
def test_only_hmac_stored(tmp_path):
    env = Env(tmp_path, codes=["482915"])
    env.send()
    otp = env.service._numbers[N].otp
    assert otp is not None
    assert "482915" not in repr(env.service._numbers)
    assert otp.code_hash == CodeHasher(b"test-secret").digest(N, otp.otp_id, "482915")
    assert len(otp.code_hash) == 32


def test_same_code_different_hash_per_issue(tmp_path):
    env = Env(tmp_path, codes=["111111", "111111"])
    env.send()
    h1 = env.service._numbers[N].otp.code_hash
    env.clock.advance(30)
    env.send()
    h2 = env.service._numbers[N].otp.code_hash
    assert h1 != h2


def test_security_log_never_contains_code(tmp_path):
    env = Env(tmp_path, codes=["654321"])
    env.send()
    env.verify("000001")
    env.verify("654321")
    text = env.settings.security_log_path.read_text("utf-8")
    assert "654321" not in text
    events = [json.loads(l)["event"] for l in text.splitlines()]
    assert events == ["otp_issued", "verify_failed", "verify_success"]


def test_security_log_rejects_code_field(env):
    try:
        env.log.write("t", "x", code="123456")
    except ValueError:
        return
    raise AssertionError("журнал должен запрещать поле code")


# ---------------------------------------------------------------- проверка
def test_expiry_boundary(env):
    env.send()
    c = env.code()
    env.clock.advance(119.999)
    assert env.verify(c) == 200
    env.clock.advance(30)
    assert env.send() == 200
    env.clock.advance(120)
    assert env.verify(env.code()) == 410


def test_lock_on_fifth_wrong_and_window(env):
    env.send()
    c = env.code()
    wrong = "000000" if c != "000000" else "000001"
    assert [env.verify(wrong) for _ in range(5)] == [401, 401, 401, 401, 423]
    assert env.verify(c) == 423
    assert env.send() == 423
    env.clock.advance(899.999)
    assert env.send() == 423
    env.clock.advance(0.001)
    assert env.send() == 200
    assert env.verify(env.code()) == 200


def test_ip_limit_counts_all_requests_and_isolated_per_ip(env):
    for _ in range(20):
        assert env.service.verify(None, "192.0.2.10").status == 400
    assert env.service.verify({"msisdn": N, "code": "123456"}, "192.0.2.10").status == 429
    assert env.service.verify({"msisdn": N, "code": "123456"}, "198.51.100.20").status == 401


def test_failed_send_keeps_active_code(env):
    env.send()
    c = env.code()
    env.clock.advance(10)
    assert env.send() == 429
    assert env.verify(c) == 200


def test_progressive_delay_after_third_is_120(env):
    """Выдачи в 0, 450, 510. В 600 первая уходит из окна, но после 3-й
    действует задержка 120 с — следующий send возможен только в 630."""
    assert env.send() == 200
    env.clock.advance(450)
    assert env.send() == 200
    env.clock.advance(60)
    assert env.send() == 200
    env.clock.advance(90)          # t = 600
    assert env.send() == 429
    env.clock.advance(29.999)      # t = 629.999
    assert env.send() == 429
    env.clock.advance(0.001)       # t = 630
    assert env.send() == 200


def test_retry_after_header(env):
    env.send()
    env.clock.advance(10)
    r = env.service.send({"msisdn": N})
    assert r.status == 429 and r.headers["Retry-After"] == "20"


# ---------------------------------------------------------------- гонки
def test_parallel_correct_verify_exactly_one_success(env):
    env.send()
    c = env.code()
    barrier = threading.Barrier(50)

    def attempt(_):
        barrier.wait()
        return env.service.verify({"msisdn": N, "code": c}, f"10.0.0.{_ % 5}").status

    with ThreadPoolExecutor(max_workers=50) as pool:
        statuses = Counter(pool.map(attempt, range(50)))
    assert statuses == {200: 1, 401: 49}


def test_parallel_wrong_codes_lock_exactly_at_five(env):
    env.send()
    c = env.code()
    wrong = [f"{(int(c) + i) % 1_000_000:06d}" for i in range(1, 11)]
    barrier = threading.Barrier(10)

    def attempt(code):
        barrier.wait()
        return env.service.verify({"msisdn": N, "code": code}, "192.0.2.10").status

    with ThreadPoolExecutor(max_workers=10) as pool:
        statuses = Counter(pool.map(attempt, wrong))
    assert statuses == {401: 4, 423: 6}
