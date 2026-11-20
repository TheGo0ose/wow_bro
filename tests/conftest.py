from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from otp_service.app import build_service, create_app  # noqa: E402
from otp_service.clock import FakeClock  # noqa: E402
from otp_service.codes import SequenceCodeSource  # noqa: E402
from otp_service.config import Settings  # noqa: E402
from otp_service.delivery import MemoryDelivery  # noqa: E402
from otp_service.security_log import SecurityLog  # noqa: E402
from otp_service.service import OtpService  # noqa: E402
from otp_service.storage import MemoryStore, RedisStore  # noqa: E402

START = "2026-11-20T12:00:00+06:00"
STORES = ["memory", "redis"]


def make_settings(tmp_path: Path, **overrides) -> Settings:
    s = Settings()
    s.secret = b"test-secret"
    s.outbox_path = tmp_path / "outbox" / "sms.jsonl"
    s.security_log_path = tmp_path / "logs" / "security.jsonl"
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


def fake_redis():
    """Отдельный in-process Redis (fakeredis с настоящим Lua-интерпретатором)."""
    fakeredis = pytest.importorskip("fakeredis")
    pytest.importorskip("lupa")
    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


def make_store(kind: str, settings: Settings):
    if kind == "redis":
        return RedisStore(fake_redis(), settings.limits(), settings.redis_prefix)
    return MemoryStore(settings.limits())


class Env:
    """Сервис с подменёнными часами и доставкой — для unit-тестов."""

    def __init__(self, tmp_path: Path, codes=None, store_kind: str = "memory", **overrides) -> None:
        self.settings = make_settings(tmp_path, **overrides)
        self.clock = FakeClock(START)
        self.delivery = MemoryDelivery()
        self.log = SecurityLog(self.settings.security_log_path)
        self.store = make_store(store_kind, self.settings)
        kwargs = {"code_source": SequenceCodeSource(codes)} if codes is not None else {}
        self.service = OtpService(self.settings, self.clock, self.delivery, self.log,
                                  store=self.store, **kwargs)

    def send(self, msisdn="996770730000"):
        return self.service.send({"msisdn": msisdn}, "127.0.0.1").status

    def verify(self, code, msisdn="996770730000", ip="192.0.2.10"):
        return self.service.verify({"msisdn": msisdn, "code": code}, ip).status

    def code(self, msisdn="996770730000"):
        return self.delivery.last_code(msisdn)


@pytest.fixture(params=STORES)
def store_kind(request):
    return request.param


@pytest.fixture
def make_env(tmp_path, store_kind):
    return lambda codes=None, **kw: Env(tmp_path, codes, store_kind, **kw)


@pytest.fixture
def env(make_env):
    return make_env()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session", params=STORES)
def live_server(request, tmp_path_factory):
    """Настоящий HTTP-сервер (uvicorn в потоке) в тестовом режиме — на каждом хранилище."""
    tmp = tmp_path_factory.mktemp(f"live-{request.param}")
    # Без доверенного заголовка: IP только из соединения, как в режиме жюри.
    settings = make_settings(tmp, test_mode=True, test_start_time=START, storage=request.param)
    redis_client = fake_redis() if request.param == "redis" else None
    app = create_app(settings, service=build_service(settings, redis_client))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           proxy_headers=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("сервер не стартовал")
        time.sleep(0.05)
    yield {"base_url": f"http://127.0.0.1:{port}", "outbox": settings.outbox_path,
           "security_log": settings.security_log_path, "app": app, "storage": request.param}
    server.should_exit = True
    thread.join(timeout=5)
