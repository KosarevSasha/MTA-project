# MTA-project

Формальное описание проекта.

## 1. Общие сведения

| Параметр | Значение |
|---|---|
| Название пакета | `mta-project` v0.1.0 (автор: syncro128) |
| Назначение | Серверная часть системы мониторинга торговых автоматов (MTA). Автоматы отправляют на сервер события и heartbeat, оператор просматривает состояние через веб-панель. |
| Язык и версия | Python ≥ 3.14 (`.python-version`: 3.14) |
| Менеджер проекта | `uv` (`uv.lock`, сборка через `uv_build`) |
| Зависимости | `fastapi`, `uvicorn`, `python-multipart` (формы логина) |
| СУБД | SQLite3, файл `mta.db` (проект мигрировал с PostgreSQL) |
| Версии компонентов | Сервер — «v1.5, stage 2»; монитор — «MTA Monitor 1.0»; SQL-скрипт ошибок — «v1.3» |

## 2. Структура репозитория

```
MTA-project/
├── pyproject.toml          # метаданные, зависимости, сборка
├── uv.lock, .python-version, .gitignore
├── README.md               # этот файл
├── mta_schema.sql          # базовая схема БД
├── mta_errors.sql          # расширение схемы: таблица device_errors
├── mta.db                  # SQLite-база (закоммичена как временная)
└── src/mta_project/
    ├── __init__.py         # пустой
    ├── mta_server.py       # FastAPI-приложение
    ├── start_mta_server.py # локальный интерактивный лаунчер
    └── vending_emulator.py # эмулятор торгового автомата (п. 6)
```

## 3. Модель данных (SQLite)

Все времена хранятся как фиксированный ISO 8601 UTC-текст
(`YYYY-MM-DDTHH:MM:SS.ffffffZ`), поэтому строковое сравнение совпадает
с хронологическим. Внешние ключи включаются на каждом соединении
(`PRAGMA foreign_keys = ON`).

### `mta_schema.sql` (применять первым)

- `devices` — автоматы: `device_id` (PK), `name`, `location`,
  `machine_state` (по умолчанию `IDLE`), `active_error` (по умолчанию `NONE`),
  `last_seen`.
- `products` — справочник товаров: `product_id` (PK), `name`.
- `events` — журнал событий устройств: `event_id` (PK, ключ идемпотентности),
  `device_id` (FK), `event_type`, `session_id`, `created_at`,
  `data` (JSON, проверка `json_valid`).
- `inventory` — остатки: PK `(device_id, product_id)`,
  `quantity` с `CHECK (quantity >= 0)`.
- `sales` — продажи: `sale_id` (autoincrement), `session_id` (UNIQUE),
  FK на устройство, товар и событие (`event_id` UNIQUE), `price > 0`,
  `payment`, `dispense`, `sold_at`. Индекс `sales_device_time (device_id, sold_at DESC)`.

### `mta_errors.sql` (применять вторым)

- `device_errors` — интервалы неисправностей: `error_code`, `occurred_at`,
  `cleared_at`, `set_event_id` и `clear_event_id` (UNIQUE, FK на `events`),
  `details` (JSON).
- Ограничения: `cleared_at >= occurred_at`; `cleared_at` и `clear_event_id`
  либо оба NULL, либо оба заданы.
- Частичный уникальный индекс `(device_id, error_code) WHERE cleared_at IS NULL`:
  не более одной активной ошибки каждого кода на устройство.
- Индекс `device_errors_device_timeline (device_id, occurred_at DESC)`.

Инициализация базы:

```
sqlite3 mta.db < mta_schema.sql
sqlite3 mta.db < mta_errors.sql
```

## 4. Серверное приложение (`mta_server.py`)

FastAPI-приложение; Swagger, ReDoc и OpenAPI отключены. Соединение с БД
открывается на каждый запрос (`get_db_connection()`): WAL-режим,
`row_factory=Row`, commit или rollback, затем закрытие.

### 4.1. Аутентификация устройств

Заголовок `X-MTA-Key` проверяется по JSON-словарю `MTA_DEVICE_KEYS`
(`{device_id: ключ}`):

- ключ не короче 32 символов;
- сравнение через `hmac.compare_digest`;
- при отсутствии или неверной конфигурации — 503 (fail closed), при неверном ключе — 401.

### 4.2. Аутентификация оператора

- Единственный пользователь — `admin`. Пароль проверяется по хешу
  `pbkdf2_sha256$раунды$соль_hex$хеш_hex` из `MTA_ADMIN_PASSWORD_HASH`
  (раундов не менее 200 000).
- Сессия — подписанная cookie `mta_operator_session`: содержимое
  `admin:<expiry>:<nonce>`, подпись HMAC-SHA256 ключом `MTA_SESSION_SECRET`
  (не короче 40 символов), срок жизни 8 часов. Флаги: `HttpOnly`,
  `SameSite=Strict`, `Secure` при `MTA_HTTPS=1`.
- Защита от CSRF — проверка `Origin` / `Referer`.
- Защита от перебора: не более 5 неудачных попыток за 300 с с одного IP
  (счётчик в памяти процесса).

### 4.3. HTTP-интерфейс

| Метод и путь | Доступ | Назначение |
|---|---|---|
| `GET/POST /login`, `POST /logout` | публичный, с проверкой origin | вход и выход оператора |
| `POST /api/v1/device/heartbeat` | ключ устройства | обновляет `machine_state`, `last_seen`, `active_error`; при незакрытых записях в `device_errors` берётся самая свежая, иначе переданная ошибка (только при `state='ERROR'`); 404, если устройства нет |
| `POST /api/v1/device/events` | ключ устройства | приём событий (п. 4.4) |
| `GET /api/v1/devices/{id}` | оператор | краткий статус; `ONLINE`, если heartbeat не старше 30 с |
| `GET /api/v1/monitor/{id}` | оператор | устройство, остатки, 10 последних продаж и ошибок |
| `GET /monitor` | оператор (иначе редирект на `/login`) | HTML-дашборд, обновление каждые 10 с, привязан к `MTA-001` |

### 4.4. Обработка событий

Допустимые `event_type`: `SALE`, `LOAD`, `UNLOAD`, `ERROR_SET`, `ERROR_CLEAR`.
`created_at` обязан содержать часовой пояс. Для `SALE` обязательны
`session_id`, `data.product_id`, положительная `data.price`,
`payment=SUCCESS` и `dispense=SUCCESS`.

Обработка идемпотентна и выполняется в одной транзакции:

1. `INSERT ... ON CONFLICT (event_id) DO NOTHING`; повтор возвращает `already_accepted`.
2. Действие по типу:
   - `SALE` — запись в `sales` и атомарное списание единицы остатка;
   - `LOAD` — увеличение остатка;
   - `UNLOAD` — уменьшение остатка с проверкой достаточности;
   - `ERROR_SET` — открытие записи в `device_errors`, установка `devices.active_error`;
   - `ERROR_CLEAR` — закрытие активной ошибки и пересчёт `active_error`
     по оставшимся открытым (иначе `NONE`).
3. Бизнес-ошибки и нарушения целостности дают 400 со структурой
   `{status, event_id, reason}`; прочие ошибки БД — 503.

## 5. Запуск

`start_mta_server.py` — интерактивный лаунчер для локального тестирования
(для интернета не предназначен):

- ключ устройства `MTA-001` (≥ 32 символов) и пароль администратора
  (≥ 20 символов) читаются из буфера обмена через `tkinter` (из KeePassXC);
- вычисляются PBKDF2-хеш (310 000 раундов, случайная соль) и случайный
  `MTA_SESSION_SECRET`;
- сервер стартует через `uvicorn` на `http://127.0.0.1:8001/login`.

Конфигурация задаётся переменными окружения:

| Переменная | Значение |
|---|---|
| `MTA_DEVICE_KEYS` | JSON `{device_id: ключ}` |
| `MTA_ADMIN_PASSWORD_HASH` | `pbkdf2_sha256$раунды$соль$хеш` |
| `MTA_SESSION_SECRET` | секрет подписи сессий, ≥ 40 символов |
| `MTA_DB_PATH` | путь к SQLite-файлу (по умолчанию `mta.db`) |
| `MTA_HTTPS` | `1` — включить флаг `Secure` для cookie |

## 6. Эмулятор торгового автомата (`vending_emulator.py`)

Консольный клиент для тестирования сервера: имитирует автомат, который
отправляет heartbeat и события. Только стандартная библиотека.

```
uv run python -m mta_project.vending_emulator seed    # один раз
export MTA_DEVICE_KEY=<ключ из MTA_DEVICE_KEYS>
uv run python -m mta_project.vending_emulator check
uv run python -m mta_project.vending_emulator run --interval 1
```

| Подкоманда | Назначение |
|---|---|
| `seed` | добавляет в SQLite автомат, 4 товара (`COLA`, `WATER`, `CHIPS`, `CHOCO`) и нулевые остатки; остатки обязательны, иначе сервер отвечает на `LOAD` 400 «Inventory record not found» |
| `run` | непрерывная имитация: начальный `LOAD` по 10 шт., heartbeat, случайные продажи, `ERROR_SET`/`ERROR_CLEAR`, дозагрузка при пустом остатке; при открытой ошибке продаж нет, состояние `ERROR` |
| `check` | детерминированный набор проверок API (код возврата 0/1) |

Параметры `run`: `--duration` (с, 0 — до Ctrl+C), `--interval` (пауза
между действиями), `--heartbeat` (период, по умолчанию 10 с),
`--error-rate`, `--duplicate-rate` (доля повторных отправок продаж для
проверки идемпотентности), `--seed` (воспроизводимость).

`check` проверяет: heartbeat без ключа и с неверным ключом (401);
принятую продажу и её повтор (`already_accepted`); дубликат `session_id`,
`payment=FAILED`, цену 0, неизвестный товар, `created_at` без часового
пояса, неизвестный `event_type`, `UNLOAD` сверх остатка (всё — 400);
цикл `ERROR_SET`/`ERROR_CLEAR`; неизвестное устройство.

Общие параметры (флаг или переменная окружения): `--url` / `MTA_URL`
(по умолчанию `http://127.0.0.1:8001`), `--device-id` / `MTA_DEVICE_ID`
(`MTA-001`), `--key` / `MTA_DEVICE_KEY` (обязателен для `run` и `check`),
`--db` / `MTA_DB_PATH` (только для `seed`, по умолчанию `mta.db`).

Особенность сервера, зафиксированная в `check`: повторный `ERROR_SET` с тем
же кодом при открытой ошибке принимается (200) как no-op.

## 7. Известные ограничения

1. Скрипт `mta-project = "mta_project:main"` в `pyproject.toml` указывает на
   несуществующую функцию (`__init__.py` пуст; лаунчер — `start_mta_server.main`).
2. Файл `mta.db` закоммичен и не исключён в `.gitignore`.
3. В `receive_event` вызов `logger.exception("Event database failure: ?", ...)`
   использует `?` вместо `%s`.
4. Монитор жёстко привязан к `MTA-001`; единственный пользователь — `admin`.
5. Счётчик попыток входа хранится в памяти процесса (не работает при нескольких воркерах).
6. `GET /api/v1/devices/{id}` для неизвестного устройства возвращает 200 со
   `status: not_found`, а `/api/v1/monitor/{id}` — 404.
7. Тестов, CI и миграций нет; схема восстановлена вручную по запросам сервера.
