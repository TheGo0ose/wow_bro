from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from app.api.otp import router as otp_router
from app.api.security import router as security_router
from app.api.telecom import router as telecom_router
from app.api.test_control import router as test_control_router
from app.config import get_settings
from app.core.redis_client import close_redis, get_redis
from app.services.billing_service import ensure_balances
from app.services.sms_dispatcher import sms_dispatcher

logger = logging.getLogger("otp_shield.main")

STATIC_INDEX_PATH = Path(__file__).resolve().parent / "static" / "index.html"


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _is_telecom_enabled() -> bool:
    raw = os.environ.get("TELECOM_ENABLED")
    if raw is None:
        raw = get_settings().TELECOM_ENABLED
    return raw.strip().lower() != "false"


def _is_test_mode() -> bool:
    if os.environ.get("TEST_MODE", "").strip().lower() == "true":
        return True
    return bool(get_settings().TEST_MODE)


async def _periodic_telecom_probe() -> None:
    while True:
        await asyncio.sleep(5.0)
        with suppress(Exception):
            await sms_dispatcher.probe_gateway()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Initialize Redis, billing balances, and optional telecom health probe."""
    probe_task: asyncio.Task[None] | None = None
    try:
        redis_client = await _maybe_await(get_redis())
        try:
            await ensure_balances(redis_client)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to initialize billing balances: %s", type(exc).__name__)

        if _is_telecom_enabled():
            with suppress(Exception):
                await asyncio.wait_for(sms_dispatcher.probe_gateway(), timeout=0.6)
            probe_task = asyncio.create_task(_periodic_telecom_probe())

        yield
    finally:
        if probe_task is not None:
            probe_task.cancel()
            with suppress(asyncio.CancelledError):
                await probe_task
        with suppress(Exception):
            await _maybe_await(close_redis())


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    application = FastAPI(
        title="OTP Shield",
        version="1.0.0",
        lifespan=lifespan,
    )

    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        sanitized_errors = []
        for err in exc.errors():
            item = dict(err)
            ctx = item.get("ctx")
            if isinstance(ctx, dict):
                item["ctx"] = {
                    k: str(v) if isinstance(v, BaseException) else v
                    for k, v in ctx.items()
                }
            sanitized_errors.append(item)
        return JSONResponse(
            status_code=422,
            content={
                "detail": "VALIDATION_ERROR",
                "errors": jsonable_encoder(sanitized_errors),
            },
        )

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(
        _request: Request,
        exc: Exception,
    ) -> JSONResponse:
        logger.error("Unhandled exception during request: %s", type(exc).__name__)
        return JSONResponse(
            status_code=500,
            content={"detail": "INTERNAL_ERROR"},
        )

    @application.get("/health")
    async def health() -> JSONResponse:
        telecom_status = sms_dispatcher.get_status()
        try:
            redis_client = await _maybe_await(get_redis())
            await redis_client.ping()
        except Exception:  # noqa: BLE001
            return JSONResponse(
                status_code=503,
                content={
                    "status": "degraded",
                    "redis": "down",
                    "telecom": telecom_status,
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "redis": "up",
                "telecom": telecom_status,
            },
        )

    @application.get("/", response_model=None)
    async def index() -> FileResponse | JSONResponse:
        if STATIC_INDEX_PATH.is_file():
            return FileResponse(STATIC_INDEX_PATH)
        return JSONResponse(
            status_code=200,
            content={"service": "otp-shield", "status": "ok"},
        )

    application.include_router(otp_router)
    application.include_router(security_router)
    application.include_router(telecom_router)
    if _is_test_mode():
        application.include_router(test_control_router)

    return application


app = create_app()
