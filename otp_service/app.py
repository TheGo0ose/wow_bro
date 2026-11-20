"""HTTP-слой (FastAPI). Вся логика — в service.py."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .clock import FakeClock, SystemClock
from .config import Settings
from .dashboard import DASHBOARD_HTML
from .delivery import OutboxDelivery
from .security_log import SecurityLog
from .service import OtpService, Result
from .storage import MemoryStore, RedisStore, Store

_INVALID_JSON = object()


async def _read_json(request: Request) -> Any:
    raw = await request.body()
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _INVALID_JSON


def _respond(result: Result) -> JSONResponse:
    return JSONResponse(result.body, status_code=result.status, headers=result.headers)


def build_store(settings: Settings, redis_client: Any = None) -> Store:
    if settings.storage == "redis" or redis_client is not None:
        if redis_client is not None:
            return RedisStore(redis_client, settings.limits(), settings.redis_prefix)
        return RedisStore.from_url(settings.redis_url, settings.limits(), settings.redis_prefix)
    return MemoryStore(settings.limits())


def build_service(settings: Settings, redis_client: Any = None) -> OtpService:
    clock = FakeClock(settings.test_start_time) if settings.test_mode else SystemClock()
    return OtpService(
        settings=settings,
        clock=clock,
        delivery=OutboxDelivery(settings.outbox_path),
        security_log=SecurityLog(settings.security_log_path),
        avoid_repeat_code=settings.test_mode,
        store=build_store(settings, redis_client),
    )


def create_app(settings: Settings | None = None, service: OtpService | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    service = service or build_service(settings)

    app = FastAPI(title="INAI OTP service", version="1.0.0")
    app.state.service = service
    app.state.settings = settings

    def client_ip(request: Request) -> str:
        """IP из соединения. Заголовку верим только если он явно включён в конфиге
        и запрос пришёл от доверенного прокси. X-Forwarded-For не читается."""
        peer = request.client.host if request.client else "unknown"
        header = settings.trusted_ip_header
        if header and peer in settings.trusted_proxies:
            value = request.headers.get(header, "").strip()
            if value:
                return value
        return peer

    def require_loopback(request: Request) -> None:
        peer = request.client.host if request.client else ""
        if not settings.test_mode or peer not in settings.trusted_proxies:
            raise HTTPException(status_code=404)

    # Ошибки фреймворка тоже отдаём в виде JSON.
    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": "not_found" if exc.status_code == 404 else "error"},
                            status_code=exc.status_code)

    # OtpService потокобезопасен: проверка и погашение кода идут под одной
    # блокировкой, поэтому атомарность не зависит от модели сервера.
    @app.post("/otp/send")
    async def otp_send(request: Request) -> JSONResponse:
        payload = await _read_json(request)
        return _respond(service.send(None if payload is _INVALID_JSON else payload,
                                     client_ip(request)))

    @app.post("/otp/verify")
    async def otp_verify(request: Request) -> JSONResponse:
        payload = await _read_json(request)
        return _respond(service.verify(None if payload is _INVALID_JSON else payload,
                                       client_ip(request)))

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "test_mode": settings.test_mode, "storage": service.store.name}

    if settings.dashboard_enabled:
        @app.get("/dashboard", response_class=HTMLResponse)
        def dashboard() -> str:
            return DASHBOARD_HTML

        @app.get("/security/events")
        def security_events(limit: int = 100) -> dict[str, Any]:
            limit = max(1, min(limit, 500))
            return {"counts": service.log.counts(), "events": service.log.recent(limit)}

    # ---- служебные методы тестового режима (только OTP_TEST_MODE=1 и loopback) ----
    # Они управляют часами и сбрасывают состояние; код они НЕ возвращают.
    @app.post("/__test__/advance")
    async def test_advance(request: Request) -> dict[str, Any]:
        require_loopback(request)
        payload = await _read_json(request)
        seconds = payload.get("seconds") if isinstance(payload, dict) else None
        if not isinstance(seconds, (int, float)) or seconds < 0:
            raise HTTPException(status_code=400)
        now = service.clock.advance(float(seconds))  # type: ignore[attr-defined]
        return {"now": service.clock.iso(now)}

    @app.post("/__test__/reset")
    def test_reset(request: Request) -> dict[str, Any]:
        require_loopback(request)
        service.reset()        # сбрасывает и хранилище (в т.ч. ключи Redis с нашим префиксом)
        service.clock.reset()  # type: ignore[attr-defined]
        service.log.clear_memory()
        return {"now": service.clock.iso()}

    return app
