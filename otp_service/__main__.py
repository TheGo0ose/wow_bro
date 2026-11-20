"""Запуск: python -m otp_service  (хост/порт: OTP_HOST, OTP_PORT; хранилище: OTP_STORAGE)."""

import os
import sys

import uvicorn

from .app import create_app
from .config import Settings


def main() -> None:
    host = os.environ.get("OTP_HOST", "127.0.0.1")
    port = int(os.environ.get("OTP_PORT", "8000"))
    settings = Settings.from_env()
    try:
        app = create_app(settings)
    except Exception as exc:  # noqa: BLE001 — показываем причину без traceback
        if settings.storage == "redis":
            sys.exit(f"Не удалось подключиться к Redis ({settings.redis_url}): {exc}\n"
                     "Запустите Redis или уберите OTP_STORAGE=redis (будет хранилище в памяти).")
        raise
    print(f"OTP-сервис: http://{host}:{port}  хранилище={settings.storage}"
          f"{'  [ТЕСТОВЫЙ РЕЖИМ]' if settings.test_mode else ''}", flush=True)
    # proxy_headers=False: иначе uvicorn сам подставляет IP из X-Forwarded-For
    # (по умолчанию доверяя 127.0.0.1) и IP-лимит обходится подменой заголовка.
    # Один воркер: при OTP_STORAGE=memory состояние живёт в памяти процесса.
    # С Redis можно запускать несколько процессов/реплик — состояние общее.
    uvicorn.run(app, host=host, port=port, workers=1, proxy_headers=False)


if __name__ == "__main__":
    main()
