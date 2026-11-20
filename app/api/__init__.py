from __future__ import annotations

from app.api.otp import router as otp_router
from app.api.security import router as security_router
from app.api.telecom import router as telecom_router
from app.api.test_control import router as test_control_router

__all__ = [
    "otp_router",
    "security_router",
    "telecom_router",
    "test_control_router",
]
