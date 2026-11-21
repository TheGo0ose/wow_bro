# OTP Shield (ТЗ 6)

Защищённый OTP-сервис + SOC-дашборд в реальном времени + опциональный телеком-контур (SMPP v3.4 + RabbitMQ + биллинг и DLR).

---

## Быстрый запуск в Docker (1 команда)

### 1. Базовый контур (для автопроверки жюри)

Поднимает только `otp-api` (порт `:8000`) и `redis:8-alpine` (`:6379`) без persistent volume (чистое состояние при каждом запуске) и с bind-mount `./outbox:/app/outbox` для чтения `outbox/sms.jsonl` с хоста:

```bash
docker compose up --build -d
```

Проверка готовности:

```bash
curl -fsS http://localhost:8000/health
```

SOC-дашборд доступен в браузере по адресу: `http://localhost:8000/`.

### 2. Полный контур с телеком-шлюзом (`--profile telecom`)

Поднимает базовый контур + `otp-rabbitmq` (`rabbitmq:4-management-alpine`, AMQP `:5672`, UI `:15672`) + `otp-postgres` (`postgres:18-alpine`, `:5432`) + `otp-telecom-gateway` (`infra/telecom_smsc.py`, SMPP v3.4 `:2775`, HTTP API `:1401`). Благодаря `TELECOM_ENABLED=auto` сервис `otp-api` автоматически обнаруживает `otp-telecom-gateway` и переходит в статус `BOUND_TRX` без дополнительных флагов. Весь биллинг, HLR/MNP-маршрутизация и SQL-транзакции изолированы на стороне оператора:

```bash
docker compose --profile telecom up --build -d
```

Остановка и очистка:

```bash
docker compose --profile telecom down
```

### 3. Локальный запуск (Python `.venv` + Redis)

Требования: Python `3.11+` (или виртуальное окружение `.venv`) и запущенный локальный Redis (`redis://localhost:6379/0`).

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Для запуска локального телеком-шлюза дополнительно (при поднятом RabbitMQ или в fallback-режиме):

```bash
.venv/bin/python -m infra.telecom_smsc
```

---

## Архитектура и механизмы защиты

```text
  Клиент / Автотест / attack.py
         │
         ▼
  ┌──────────────────────────────────────────────────────────┐
  │ otp-api (FastAPI, app/main.py, app/api/*)                │
  │  • POST /otp/send, POST /otp/verify                      │
  │  • GET / (SOC-дашборд), /security/stats, /security/stream│
  │  • Thin proxy: /telecom/status, /bulk, /topup, /hlr      │
  │  • Webhook: POST /telecom/dlr (без записи в audit log)   │
  │  • Zero SQL / Zero billing / Zero HLR logic              │
  └───────────────┬──────────────────────┬───────────────────┘
                  │                      │ (фоновый, aio-pika / HTTP)
                  ▼                      ▼
  ┌───────────────────────────┐   ┌──────────────────────────┐
  │ Redis + Lua (атомарно)    │   │ Outbox + AMQP            │
  │  • SEND_OTP_LUA           │   │  • outbox/sms.jsonl      │
  │  • VERIFY_OTP_LUA         │   │  • Circuit Breaker       │
  │  • Stream security:events │   │  • otp-rabbitmq (:5672)  │
  └───────────────────────────┘   └──────────────┬───────────┘
                                                 │ smsc.otp_svc / smsc.bulk_mkt
                                                 ▼
                                  ┌──────────────────────────┐
                                  │ otp-telecom-gateway      │
                                  │  • SMPP v3.4 (:2775)     │
                                  │  • HLR/MNP (infra/hlr.py)│
                                  │  • CCN 402 + ACID Billing│
                                  └──────────────┬───────────┘
                                                 │ asyncpg (ACID)
                                                 ▼
                                  ┌──────────────────────────┐
                                  │ otp-postgres (PG 18)     │
                                  │  • sdp_accounts/cdrs/hlr │
                                  └──────────────────────────┘
```

### Криптография и конфиденциальность
- **Генерация кода:** строго через `secrets.randbelow(1_000_000)` в формате 6 цифр (`000000`–`999999`), без использования `random`.
- **Хеширование:** в Redis хранится только `HMAC-SHA256(HMAC_SECRET, "{msisdn}:{code}")`. Исходные OTP-коды не сохраняются в Redis, не возвращаются в HTTP-ответах и не попадают в аудит-логи (`security:events` и stdout).

### Атомарность в Redis Lua (`SEND_OTP_LUA`, `VERIFY_OTP_LUA`)
Вся логика проверки лимитов, блокировок и верификации выполняется внутри одного атомарного Lua-вызова в Redis с передачей миллисекундной метки времени `now_ms`:
- **Защита от Race Condition (`O16`):** при 20–50 параллельных `POST /otp/verify` с верным кодом первый запрос атомарно помечает `status = "used"` и возвращает `200 OK`, а все остальные получают `401 Unauthorized` без инкремента счётчика ошибок `fail:{msisdn}`.
- **Точные миллисекундные скользящие окна:** проверка задержек выполняется в миллисекундах (`29.999` с отклоняется с `429`, `30.000` с пропускается).
- **Граница истечения (`410 Gone` vs `401 Unauthorized`):** строгое условие `now_ms >= expires_at_ms` (ровно на `120.0` с возвращается `410 Gone`). Запись `otp:{msisdn}` хранится с `OTP_KEY_TTL = 3600` с (по умолчанию), чтобы надёжно отличать просроченный код (`410`) от отсутствующего/неверного (`401`). Повторный `/otp/send` перезаписывает и аннулирует предыдущий код.
- **Сравнение хешей за постоянное время:** побайтный XOR в Lua (`bit.bxor` + `bit.bor` по всей длине 64-символьного hex-хеша без раннего выхода).
- **Прогрессивные задержки и квота отправки:** задержки `30 / 60 / 120` с между отправками и жёсткий лимит `3` отправки за `10` минут (`600` с) на один `msisdn` с заголовком `Retry-After`.
- **Блокировка номера (`423 Locked`):** после `5` неверных попыток `msisdn` блокируется на `15` минут (`900` с); блокировка действует и на `POST /otp/verify`, и на `POST /otp/send`.
- **IP-лимит (`429 Too Many Requests`):** не более `20` проверок `/otp/verify` за `60` с с одного IP по всем номерам. IP извлекается из `X-Forwarded-For` (первый адрес) → `X-Real-IP` → `X-Client-IP` → `request.client.host`.

### Межсервисный транспорт доставки (`OTP_OUTBOX`)
На каждую успешную выдачу `POST /otp/send` сервис дописывает строку JSON в файл, заданный `OTP_OUTBOX` (по умолчанию `outbox/sms.jsonl`, в Docker проброшен через `./outbox:/app/outbox`):

```json
{"ts": "2026-11-21T09:00:00+06:00", "msisdn": "996770730000", "code": "012345"}
```

Сервис использует этот файл исключительно на дозапись (append + flush) для тестового перехвата доставки и никогда его не читает.

### Телеком-контур и SOC-дашборд
- **SOC-дашборд (`GET /`):** тёмная тема, KPI-счётчики, живая SSE-лента событий (`GET /security/stream`) с отдельным SSE-событием `telecom_dlr` (мгновенно обновляет панель телекома без замусоривания Security Audit Stream) и панель телеком-канала (`SDP: PostgreSQL 18 (SDP-07 ACID)` или `STANDBY (Operator Offline)`).
- **Чистая граница оператора (`otp-api` ↔ `otp-rabbitmq` ↔ `otp-telecom-gateway` ↔ `otp-postgres`):** в `otp-api` полностью отсутствуют SQL/биллинг/HLR-логика. `otp-api` публикует OTP в очередь `smsc.otp_svc` через `aio-pika` (с TTL `OTP_TTL` и фоновым HTTP-fallback при отключённом AMQP) и проксирует управляющие запросы (`/telecom/status`, `/telecom/bulk`, `/telecom/topup`, `/telecom/hlr`, `/telecom/hlr/mnp`) на шлюз оператора (возвращая `503 TELECOM_GATEWAY_OFFLINE`, если оператор не запущен).
- **Приоритетные каналы, HLR/MNP и ACID-биллинг (CCN 402):** на стороне `otp-telecom-gateway` реализованы очереди `otp_svc` (`100 TPS`, `0.45 сом/сегм.`) и `bulk_mkt` (`5 TPS`, `0.30 сом/сегм.`), подсчёт сегментов GSM-7 (160/153) и UCS-2 (70/67), HLR/MNP-маршрутизация (`Beeline KG`, `MEGA`, `O!`, перенесённые `[MNP]` и `ABSENT_SUBSCRIBER` → `UNDELIV`), проверка баланса с HTTP `402 INSUFFICIENT_BALANCE`, отбрасывание просроченных OTP (`EXPIRED`, `0.00 сом`), идемпотентные CDR в `otp-postgres` (`SDP-07 ACID`) и DLR-коллбэки на `POST /telecom/dlr`.
- **Изоляция от ядра:** отправка через `otp-rabbitmq` / `otp-telecom-gateway` идёт асинхронно в фоне и защищена неблокирующим Circuit Breaker — недоступность телеком-контура никогда не влияет на время ответа или статус `/otp/send` и `/otp/verify`.

---

## Примеры `curl`

### Проверка здоровья сервиса
```bash
curl -sS http://localhost:8000/health | jq
```

### Отправка OTP
```bash
curl -i -sS -X POST http://localhost:8000/otp/send \
  -H "Content-Type: application/json" \
  -H "X-Forwarded-For: 192.0.2.10" \
  -d '{"msisdn": "996770730000"}'
```

### Проверка OTP (код можно посмотреть в последней строке `outbox/sms.jsonl`)
```bash
curl -i -sS -X POST http://localhost:8000/otp/verify \
  -H "Content-Type: application/json" \
  -H "X-Forwarded-For: 192.0.2.10" \
  -d '{"msisdn": "996770730000", "code": "012345"}'
```

### Статистика и аудит безопасности
```bash
curl -sS "http://localhost:8000/security/stats?limit=20" | jq
```

### Поток событий безопасности в реальном времени (SSE)
```bash
curl -N http://localhost:8000/security/stream
```

### Статус телеком-шлюза и балансы/CDR
```bash
curl -sS http://localhost:8000/telecom/status | jq
```

### Запуск тестовой массовой рассылки (`bulk_mkt`, 5 TPS)
```bash
curl -sS -X POST http://localhost:8000/telecom/bulk \
  -H "Content-Type: application/json" \
  -d '{"count": 20, "text": "Akciya! Skidki 20% tolko segodnya!"}' | jq
```

---

## Тестирование и демонстрация

### Запуск автотестов (`pytest`)
Перед локальным запуском тестов убедитесь, что Redis доступен на `localhost:6379`:

```bash
.venv/bin/pytest -v
```

В набор входят:
- `tests/test_scenarios_examples.py` — прогон открытых сценариев из `scenarios_examples.json` (`O01`, `O03`, `O07`, `O12`, `O16`) с фикстурой `test_identities.json`;
- `tests/test_otp_scenarios.py` — проверка криптографии, outbox, всех граничных условий (`29.999` с, `120.0` с), блокировок `423`, лимитов `429`, IP-лимитов, журнала безопасности и валидации;
- `tests/test_telecom_operator.py` — тестирование изолированного контура оператора (`HLR/MNP`, `CCN 402`, идемпотентный биллинг/CDR, отбрасывание просроченных OTP, `ABSENT_SUBSCRIBER` → `UNDELIV`) и границы `otp-api` (`503 STANDBY`, `POST /telecom/dlr` без загрязнения `security:events`);
- `tests/test_concurrency.py` — проверка защиты от race condition при `20` и `50` параллельных `verify`.

### Демонстрация атак (`scripts/attack.py`)
```bash
.venv/bin/python scripts/attack.py --base-url http://localhost:8000
```

Проверяет три сценария атаки (перебор на один номер → `423`, распределённый перебор по 50 номерам с одного IP → `429`, гонка 30 параллельных запросов с верным кодом → `1 × 200`, `29 × 401`) и выводит сводную таблицу с итогом `0` успешных взломов.

### Демонстрация приоритета OTP над массовой рассылкой (`scripts/bulk_flood.py`)
При запущенном профиле `telecom`:

```bash
.venv/bin/python scripts/bulk_flood.py --base-url http://localhost:8000
```

### Проверка качества кода (`ruff`)
```bash
.venv/bin/ruff check .
```

---

## Что сделано / что не успели

### Что сделано
- **Базовое ядро OTP (`app/core/`, `app/services/`, `app/api/`):** генерация через `secrets.randbelow`, хеширование `HMAC-SHA256`, атомарные Lua-скрипты в Redis (`SEND_OTP_LUA`, `VERIFY_OTP_LUA`) с постоянным временем сравнения (`bit.bxor` + `bit.bor`), миллисекундные скользящие окна, прогрессивные задержки `30/60/120` с, лимит `3` отправки за `10` минут, блокировка `423` после `5` ошибок на `15` минут (для `/verify` и `/send`), IP-лимит `20` проверок/мин, транспорт `outbox/sms.jsonl`, аудит `security:events` без утечки кодов.
- **Автотесты и пентест-инструменты:** реализованы все 5 открытых сценариев из `scenarios_examples.json`, расширенный набор сценариев в `tests/test_otp_scenarios.py`, тесты границы оператора и телеком-контура в `tests/test_telecom_operator.py`, тесты параллельности `tests/test_concurrency.py` (20 и 50 одновременных запросов), а также `scripts/attack.py` и `scripts/bulk_flood.py`.
- **SOC-дашборд:** однофайловый интерфейс `app/static/index.html` с живой SSE-лентой (`GET /security/stream` + событие `telecom_dlr`), агрегированной статистикой (`GET /security/stats`), фильтрацией по MSISDN/IP и отображением телеком-метрик (`SDP: PostgreSQL 18 (SDP-07 ACID)` / `STANDBY`).
- **Телеком-контур (профиль `telecom`):** `otp-rabbitmq` (`rabbitmq:4-management-alpine`) + `otp-postgres` (`postgres:18-alpine`) + шлюз оператора `otp-telecom-gateway` (`infra/telecom_smsc.py`, `infra/hlr.py`, `infra/billing_db.py`) с поддержкой бинарного протокола SMPP v3.4 (`:2775`), HTTP API (`:1401`), `aio-pika` очередей (`smsc.otp_svc` `100 TPS` vs `smsc.bulk_mkt` `5 TPS`), сегментации GSM-7/UCS-2, HLR/MNP, CCN `402`, идемпотентного ACID-биллинга в PostgreSQL и DLR-коллбэков (`POST /telecom/dlr`). При этом `otp-api` не содержит SQL/биллинг-зависимостей и проксирует запросы на шлюз оператора.
- **Контейнеризация и качество кода:** неблокирующий `docker compose up --build -d` (non-root пользователь, `healthcheck`, `restart: unless-stopped`), чистый `ruff check .`.

### Что не успели / осознанные упрощения
- **Вместо тяжёлых внешних образов `jookies/jasmin` + `Selenium SMPPSim` использован План Б (`infra/telecom_smsc.py`):** при проработке Docker-стека был выбран лёгкий собственный Python SMPP v3.4 + RabbitMQ-совместимый шлюз вместо внешнего `jookies/jasmin` и Java-симулятора `SMPPSim` (как и предусмотрено запасным планом в `otp.md`), чтобы избежать долгой инициализации и нестабильных `jcli`-скриптов и обеспечить быстрый детерминированный старт в рамках хакатона.
- **Масштабирование за пределы одного инстанса Redis:** используется единый узел Redis (без Redis Cluster / Sentinel), что полностью соответствует требованиям ТЗ 6 и легко расширяется через `REDIS_URL`.
