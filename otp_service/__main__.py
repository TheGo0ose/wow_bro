"""Запуск: python -m otp_service  (хост/порт: OTP_HOST, OTP_PORT)."""

import os

import uvicorn

from .app import create_app


def main() -> None:
    host = os.environ.get("OTP_HOST", "127.0.0.1")
    port = int(os.environ.get("OTP_PORT", "8000"))
    # Один процесс: состояние хранится в памяти и защищено одной блокировкой.
    # proxy_headers=False: иначе uvicorn сам подставляет IP из X-Forwarded-For
    # (по умолчанию доверяя 127.0.0.1) и IP-лимит обходится подменой заголовка.
    uvicorn.run(create_app(), host=host, port=port, workers=1, proxy_headers=False)


if __name__ == "__main__":
    main()
