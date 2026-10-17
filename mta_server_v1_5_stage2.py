"""MTA server v1.5 (stage 2): device and operator authentication (Python 3.8)."""

import hmac
import hashlib
import base64
import secrets
import time
from collections import defaultdict, deque
from urllib.parse import quote
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, Header, HTTPException, Request, Depends, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
logger = logging.getLogger("mta_server")


def require_device_key(device_id: str, supplied_key: Optional[str]) -> None:
    """Authenticate device requests against per-device keys in MTA_DEVICE_KEYS.

    Environment example: MTA_DEVICE_KEYS={"MTA-001":"long-random-secret"}
    Fail closed if configuration is missing or invalid. Never log keys.
    """
    raw = os.getenv("MTA_DEVICE_KEYS", "")
    try:
        keys = json.loads(raw)
    except (ValueError, TypeError):
        keys = None
    if not isinstance(keys, dict) or not keys:
        logger.error("Device authentication is not configured")
        raise HTTPException(status_code=503, detail="Device authentication not configured")
    expected = keys.get(device_id)
    if (not isinstance(expected, str) or len(expected) < 32
            or not isinstance(supplied_key, str)
            or not hmac.compare_digest(expected, supplied_key)):
        raise HTTPException(status_code=401, detail="Invalid device credentials")



# --- Operator login: local testing on 127.0.0.1 only. Use HTTPS + secure cookies on VPS. ---
COOKIE_NAME = "mta_operator_session"
SESSION_LIFETIME = 8 * 3600
LOGIN_ATTEMPTS = defaultdict(deque)  # Local MVP only; use shared storage on VPS.


def _b64(data):
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data):
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _operator_config():
    """Expected hash: pbkdf2_sha256$iterations$salt_hex$hash_hex."""
    digest = os.getenv("MTA_ADMIN_PASSWORD_HASH", "")
    signing_key = os.getenv("MTA_SESSION_SECRET", "")
    if not digest.startswith("pbkdf2_sha256$") or len(signing_key) < 40:
        raise HTTPException(503, "Operator authentication not configured")
    return digest, signing_key.encode("utf-8")


def _verify_password(password, stored):
    try:
        scheme, rounds, salt, expected = stored.split("$")
        if scheme != "pbkdf2_sha256" or int(rounds) < 200000:
            return False
        computed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(computed, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def _sign_session(signing_key):
    payload = "admin:{}:{}".format(int(time.time()) + SESSION_LIFETIME, secrets.token_hex(16))
    body = _b64(payload.encode("utf-8"))
    signature = _b64(hmac.new(signing_key, body.encode("ascii"), hashlib.sha256).digest())
    return body + "." + signature


def _has_session(request):
    try:
        _, signing_key = _operator_config()
        body, signature = request.cookies.get(COOKIE_NAME, "").split(".", 1)
        expected = _b64(hmac.new(signing_key, body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            return False
        role, expiry, nonce = _unb64(body).decode("utf-8").split(":", 2)
        return role == "admin" and len(nonce) == 32 and time.time() < int(expiry)
    except (ValueError, UnicodeError, TypeError):
        return False


def require_operator(request: Request):
    if not _has_session(request):
        raise HTTPException(status_code=401, detail="Operator login required")


def _same_origin(request):
    origin = request.headers.get("origin")
    if origin:
        return origin == str(request.base_url).rstrip("/")
    referer = request.headers.get("referer", "")
    return referer.startswith(str(request.base_url))


LOGIN_HTML = r'''<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Вход · MTA Monitor</title>
<style>*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0c1421;color:#eef4ff;font-family:system-ui,Segoe UI,sans-serif;padding:18px}
main{width:min(420px,100%);background:#152235;border:1px solid #30445f;border-radius:18px;padding:32px;box-shadow:0 24px 70px #050a1399}
h1{text-align:center;font-size:28px;margin:0 0 8px}.sub{text-align:center;color:#a7b8cd;margin:0 0 30px;font-size:14px}
label{display:block;margin:16px 0 7px;color:#b7cbe0;font-size:14px}input{width:100%;padding:14px;border:1px solid #435773;border-radius:10px;background:#0e1a2b;color:white;font-size:16px}
input:focus{outline:2px solid #4b96e0}button{width:100%;margin-top:26px;padding:14px;border:0;border-radius:10px;background:#3275b8;color:white;font-weight:700;font-size:16px;cursor:pointer}
.err{padding:12px;border-radius:9px;background:#542b35;color:#ffd2da;margin:12px 0}footer{text-align:center;color:#8198b2;font-size:12px;margin-top:25px}</style></head>
<body><main><h1>◉ MTA Monitor</h1><p class="sub">Панель управления торговыми автоматами</p>
ERROR_PLACEHOLDER
<form action="/login" method="post" autocomplete="on"><label for="username">Логин</label><input id="username" name="username" value="admin" autocomplete="username" required>
<label for="password">Пароль</label><input id="password" name="password" type="password" autocomplete="current-password" required>
<button type="submit">Войти в систему</button></form><footer>Доступ только для авторизованных операторов</footer></main></body></html>'''


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _has_session(request):
        return RedirectResponse("/monitor", status_code=303)
    return HTMLResponse(LOGIN_HTML.replace("ERROR_PLACEHOLDER", ""), headers={"Cache-Control": "no-store"})


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if not _same_origin(request):
        raise HTTPException(403, "Origin check failed")
    digest, signing_key = _operator_config()
    client = request.client.host if request.client else "unknown"
    now = time.monotonic()
    attempts = LOGIN_ATTEMPTS[client]
    while attempts and now - attempts[0] > 300:
        attempts.popleft()
    if len(attempts) >= 5:
        raise HTTPException(429, "Too many login attempts. Try again later")
    valid = hmac.compare_digest(username, "admin") and _verify_password(password, digest)
    if not valid:
        attempts.append(now)
        return HTMLResponse(LOGIN_HTML.replace("ERROR_PLACEHOLDER", '<div class="err">Неверный логин или пароль</div>'), status_code=401, headers={"Cache-Control": "no-store"})
    attempts.clear()
    response = RedirectResponse("/monitor", status_code=303)
    response.set_cookie(COOKIE_NAME, _sign_session(signing_key), max_age=SESSION_LIFETIME,
                        httponly=True, samesite="strict", secure=os.getenv("MTA_HTTPS", "0") == "1", path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/logout")
def logout(request: Request):
    if not _same_origin(request):
        raise HTTPException(403, "Origin check failed")
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE_NAME, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


class Heartbeat(BaseModel):
    device_id: str
    state: str
    active_error: str


class DeviceEvent(BaseModel):
    event_id: str
    device_id: str
    event_type: str
    session_id: Optional[str] = None
    created_at: datetime
    data: dict = Field(default_factory=dict)


def get_db_connection():
    return psycopg2.connect(
        host="localhost",
        port=5432,
        dbname="mta_db",
        user="postgres",
        password=os.getenv("MTA_DB_PASSWORD"),
        connect_timeout=5,
    )


def require_text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{} must be a non-empty string".format(field))
    return value


def require_positive_int(value, field):
    if type(value) is not int or value <= 0:
        raise ValueError("{} must be a positive integer".format(field))
    return value


def validate_event(event):
    require_text(event.event_id, "event_id")
    require_text(event.device_id, "device_id")

    if event.created_at.tzinfo is None or event.created_at.utcoffset() is None:
        raise ValueError("created_at must include timezone")

    if event.event_type not in (
        "SALE",
        "LOAD",
        "UNLOAD",
        "ERROR_SET",
        "ERROR_CLEAR",
    ):
        raise ValueError("unsupported event_type")

    if event.event_type in ("LOAD", "UNLOAD"):
        require_text(event.data.get("product_id"), "data.product_id")
        require_positive_int(event.data.get("quantity"), "data.quantity")

    if event.event_type in ("ERROR_SET", "ERROR_CLEAR"):
        require_text(event.data.get("error_code"), "data.error_code")

    if event.event_type == "SALE":
        require_text(event.session_id, "session_id")
        require_text(event.data.get("product_id"), "data.product_id")
        require_positive_int(event.data.get("price"), "data.price")

        if event.data.get("payment") != "SUCCESS":
            raise ValueError("SALE requires payment=SUCCESS")

        if event.data.get("dispense") != "SUCCESS":
            raise ValueError("SALE requires dispense=SUCCESS")


def bad_request(event_id, reason):
    return HTTPException(
        status_code=400,
        detail={
            "status": "bad_request",
            "event_id": event_id,
            "reason": reason,
        },
    )


@app.post("/api/v1/device/heartbeat")
def heartbeat(data: Heartbeat, x_mta_key: Optional[str] = Header(default=None)):
    require_device_key(data.device_id, x_mta_key)
    try:
        require_text(data.device_id, "device_id")
        require_text(data.state, "state")
        require_text(data.active_error, "active_error")
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error))

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE devices
                    SET machine_state = %s,
                        active_error = COALESCE(
                            (
                                SELECT error_code
                                FROM device_errors
                                WHERE device_id = %s
                                  AND cleared_at IS NULL
                                ORDER BY occurred_at DESC, error_id DESC
                                LIMIT 1
                            ),
                            CASE
                                WHEN %s = 'ERROR' THEN %s
                                ELSE 'NONE'
                            END
                        ),
                        last_seen = NOW()
                    WHERE device_id = %s
                    """,
                    (
                        data.state,
                        data.device_id,
                        data.state,
                        data.active_error,
                        data.device_id,
                    ),
                )

                if cur.rowcount == 0:
                    raise HTTPException(
                        status_code=404,
                        detail="Device not found",
                    )

    except psycopg2.Error:
        logger.exception("Heartbeat database failure")
        raise HTTPException(
            status_code=503,
            detail="Database temporarily unavailable",
        )

    return {"status": "ok"}


@app.get("/api/v1/devices/{device_id}")
def get_device(device_id: str, _operator=Depends(require_operator)):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT device_id, machine_state, active_error, last_seen
                    FROM devices
                    WHERE device_id = %s
                    """,
                    (device_id,),
                )

                row = cur.fetchone()

    except psycopg2.Error:
        logger.exception("Device lookup database failure")
        raise HTTPException(
            status_code=503,
            detail="Database temporarily unavailable",
        )

    if row is None:
        return {
            "status": "not_found",
            "device_id": device_id,
        }

    found_id, machine_state, active_error, last_seen = row

    seconds_since_last_seen = None

    if last_seen is not None:
        seconds_since_last_seen = max(
            0.0,
            (datetime.now(timezone.utc) - last_seen).total_seconds(),
        )

    communication = (
        "ONLINE"
        if (
            seconds_since_last_seen is not None
            and seconds_since_last_seen <= 30
        )
        else "OFFLINE"
    )

    return {
        "device_id": found_id,
        "communication": communication,
        "machine_state": machine_state,
        "active_error": active_error,
        "last_seen": last_seen,
        "seconds_since_last_seen": (
            round(seconds_since_last_seen, 1)
            if seconds_since_last_seen is not None
            else None
        ),
    }


@app.post("/api/v1/device/events")
def receive_event(event: DeviceEvent, x_mta_key: Optional[str] = Header(default=None)):
    require_device_key(event.device_id, x_mta_key)
    try:
        validate_event(event)
    except ValueError as error:
        raise bad_request(event.event_id, str(error))

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    INSERT INTO events (
                        event_id,
                        device_id,
                        event_type,
                        session_id,
                        created_at,
                        data
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (event_id) DO NOTHING
                    RETURNING event_id
                    """,
                    (
                        event.event_id,
                        event.device_id,
                        event.event_type,
                        event.session_id,
                        event.created_at,
                        psycopg2.extras.Json(event.data),
                    ),
                )

                if cur.fetchone() is None:
                    return {
                        "status": "already_accepted",
                        "event_id": event.event_id,
                    }

                if event.event_type == "SALE":
                    cur.execute(
                        """
                        INSERT INTO sales (
                            session_id,
                            device_id,
                            product_id,
                            price,
                            payment,
                            dispense,
                            sold_at,
                            event_id
                        )
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            event.session_id,
                            event.device_id,
                            event.data["product_id"],
                            event.data["price"],
                            event.data["payment"],
                            event.data["dispense"],
                            event.created_at,
                            event.event_id,
                        ),
                    )

                    cur.execute(
                        """
                        UPDATE inventory
                        SET quantity = quantity - 1
                        WHERE device_id = %s
                          AND product_id = %s
                          AND quantity > 0
                        RETURNING quantity
                        """,
                        (
                            event.device_id,
                            event.data["product_id"],
                        ),
                    )

                    if cur.fetchone() is None:
                        raise ValueError(
                            "Product not found or inventory is empty"
                        )

                elif event.event_type == "LOAD":
                    cur.execute(
                        """
                        UPDATE inventory
                        SET quantity = quantity + %s
                        WHERE device_id = %s
                          AND product_id = %s
                        RETURNING quantity
                        """,
                        (
                            event.data["quantity"],
                            event.device_id,
                            event.data["product_id"],
                        ),
                    )

                    if cur.fetchone() is None:
                        raise ValueError(
                            "Inventory record not found"
                        )

                elif event.event_type == "UNLOAD":
                    cur.execute(
                        """
                        UPDATE inventory
                        SET quantity = quantity - %s
                        WHERE device_id = %s
                          AND product_id = %s
                          AND quantity >= %s
                        RETURNING quantity
                        """,
                        (
                            event.data["quantity"],
                            event.device_id,
                            event.data["product_id"],
                            event.data["quantity"],
                        ),
                    )

                    if cur.fetchone() is None:
                        raise ValueError(
                            "Product not found or insufficient inventory"
                        )

                elif event.event_type == "ERROR_SET":
                    cur.execute(
                        """
                        INSERT INTO device_errors (
                            device_id,
                            error_code,
                            occurred_at,
                            set_event_id,
                            details
                        )
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (device_id, error_code)
                            WHERE cleared_at IS NULL
                        DO NOTHING
                        """,
                        (
                            event.device_id,
                            event.data["error_code"],
                            event.created_at,
                            event.event_id,
                            psycopg2.extras.Json(event.data),
                        ),
                    )

                    cur.execute(
                        """
                        UPDATE devices
                        SET active_error = %s
                        WHERE device_id = %s
                        """,
                        (
                            event.data["error_code"],
                            event.device_id,
                        ),
                    )

                elif event.event_type == "ERROR_CLEAR":
                    cur.execute(
                        """
                        UPDATE device_errors
                        SET cleared_at = %s,
                            clear_event_id = %s
                        WHERE error_id = (
                            SELECT error_id
                            FROM device_errors
                            WHERE device_id = %s
                              AND error_code = %s
                              AND cleared_at IS NULL
                            FOR UPDATE
                        )
                        RETURNING error_id
                        """,
                        (
                            event.created_at,
                            event.event_id,
                            event.device_id,
                            event.data["error_code"],
                        ),
                    )

                    if cur.fetchone() is None:
                        raise ValueError(
                            "No active fault with this error_code"
                        )

                    cur.execute(
                        """
                        UPDATE devices
                        SET active_error = COALESCE(
                            (
                                SELECT error_code
                                FROM device_errors
                                WHERE device_id = %s
                                  AND cleared_at IS NULL
                                ORDER BY occurred_at DESC, error_id DESC
                                LIMIT 1
                            ),
                            'NONE'
                        )
                        WHERE device_id = %s
                        """,
                        (
                            event.device_id,
                            event.device_id,
                        ),
                    )

        return {
            "status": "accepted",
            "event_id": event.event_id,
        }

    except ValueError as error:
        raise bad_request(event.event_id, str(error))

    except psycopg2.errors.ForeignKeyViolation:
        raise bad_request(
            event.event_id,
            "Unknown device or product",
        )

    except psycopg2.errors.UniqueViolation:
        raise bad_request(
            event.event_id,
            "Conflicting sale or session_id",
        )

    except psycopg2.errors.CheckViolation:
        raise bad_request(
            event.event_id,
            "Invalid fault timestamps or constraints",
        )

    except psycopg2.Error:
        logger.exception(
            "Event database failure: %s",
            event.event_id,
        )
        raise HTTPException(
            status_code=503,
            detail="Database temporarily unavailable",
        )

# --- MTA Monitor 1.0: read-only dashboard, using existing PostgreSQL tables ---
@app.get("/api/v1/monitor/{device_id}")
def monitor_device(device_id: str, _operator=Depends(require_operator)):
    try:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("""
                    SELECT device_id, name, location, machine_state,
                           active_error, last_seen
                    FROM devices WHERE device_id = %s
                """, (device_id,))
                device = cur.fetchone()
                if device is None:
                    raise HTTPException(status_code=404, detail="Device not found")

                cur.execute("""
                    SELECT i.product_id, i.quantity
                    FROM inventory AS i
                    WHERE i.device_id = %s
                    ORDER BY i.product_id
                """, (device_id,))
                inventory = [dict(row) for row in cur.fetchall()]

                cur.execute("""
                    SELECT session_id, product_id, price, sold_at
                    FROM sales WHERE device_id = %s
                    ORDER BY sold_at DESC, session_id DESC LIMIT 10
                """, (device_id,))
                sales = [dict(row) for row in cur.fetchall()]

                cur.execute("""
                    SELECT error_code, occurred_at, cleared_at
                    FROM device_errors WHERE device_id = %s
                    ORDER BY occurred_at DESC, error_id DESC LIMIT 10
                """, (device_id,))
                errors = [dict(row) for row in cur.fetchall()]
    except psycopg2.Error:
        logger.exception("Monitor database failure")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")

    device = dict(device)
    last_seen = device["last_seen"]
    age = (max(0.0, (datetime.now(timezone.utc) - last_seen).total_seconds())
           if last_seen is not None else None)
    device["communication"] = "ONLINE" if age is not None and age <= 30 else "OFFLINE"
    device["seconds_since_last_seen"] = round(age, 1) if age is not None else None
    return {"device": device, "inventory": inventory,
            "sales": sales, "errors": errors}


MONITOR_HTML = r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MTA Monitor 1.0</title>
<style>
:root{color-scheme:dark;font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0c1421;color:#eef4ff}
*{box-sizing:border-box}body{margin:0;padding:24px}main{max-width:1120px;margin:auto}
header{display:flex;justify-content:space-between;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:22px}
h1{margin:0;font-size:27px;letter-spacing:.3px}.sub{color:#9eb0c8;font-size:14px;margin-top:5px}
#updated{color:#9eb0c8;font-size:13px}.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px}
.card{background:#152235;border:1px solid #2b3b52;border-radius:14px;padding:20px;min-width:0}
.card .label{color:#9eb0c8;font-size:13px;margin-bottom:12px}.big{font-size:28px;font-weight:750;overflow-wrap:anywhere}
.ok{color:#65dbac}.bad{color:#ff8b8b}.warn{color:#f6ca79}.muted{color:#9eb0c8}
section{margin-top:22px}h2{font-size:18px;margin:0 0 12px}.tablewrap{overflow:auto;border:1px solid #2b3b52;border-radius:12px}
table{border-collapse:collapse;width:100%;background:#152235;font-size:14px}th,td{padding:13px 16px;text-align:left;border-bottom:1px solid #2b3b52;white-space:nowrap}th{color:#a6bad2;font-weight:600}tr:last-child td{border-bottom:0}
.empty{color:#9eb0c8}#error{display:none;margin:14px 0;padding:12px;border-radius:10px;background:#4c242a;color:#ffd4d4}
footer{color:#8297b2;font-size:12px;margin-top:24px}code{font-size:12px;color:#b3d4ff}
@media(max-width:720px){body{padding:14px}.grid{grid-template-columns:1fr}h1{font-size:23px}}
</style>
</head><body><main>
<header><div><h1>◉ MTA Monitor <span class="muted">1.0</span></h1><div class="sub" id="deviceTitle">MTA-001 · Мониторинг автомата</div></div><div id="updated">Подключение...</div></header>
<div id="error" role="alert"></div>
<div class="grid">
<div class="card"><div class="label">Связь</div><div class="big" id="communication">—</div><div class="sub" id="lastSeen">—</div></div>
<div class="card"><div class="label">Состояние МТА</div><div class="big" id="state">—</div><div class="sub" id="activeError">—</div></div>
<div class="card"><div class="label">Остаток товара</div><div class="big" id="stock">—</div><div class="sub" id="stockDetails">—</div></div>
</div>
<section><h2>Последние продажи</h2><div class="tablewrap"><table><thead><tr><th>Время</th><th>Товар</th><th>Цена</th><th>Сессия</th></tr></thead><tbody id="sales"><tr><td colspan="4" class="empty">Загрузка...</td></tr></tbody></table></div></section>
<section><h2>История ошибок</h2><div class="tablewrap"><table><thead><tr><th>Возникла</th><th>Ошибка</th><th>Устранена</th><th>Статус</th></tr></thead><tbody id="errors"><tr><td colspan="4" class="empty">Загрузка...</td></tr></tbody></table></div></section>
<form action="/logout" method="post" style="text-align:right;margin-top:18px"><button type="submit" style="background:#263b54;border:1px solid #49617c;color:#eef4ff;border-radius:9px;padding:10px 20px;cursor:pointer">Выйти</button></form>
<footer>Только просмотр · Обновление каждые 10 секунд · Время отображается в часовом поясе браузера</footer>
</main><script>
'use strict';
const id = 'MTA-001';
const el = name => document.getElementById(name);
const date = v => v ? new Date(v).toLocaleString('ru-RU') : '—';
function cell(row, value, cls){const td=document.createElement('td');td.textContent=String(value ?? '—');if(cls)td.className=cls;row.appendChild(td)}
function table(name, items, cols, build){const body=el(name);body.replaceChildren();if(!items.length){const tr=document.createElement('tr');const td=document.createElement('td');td.colSpan=cols;td.className='empty';td.textContent='Записей пока нет';tr.appendChild(td);body.appendChild(tr);return}for(const item of items){const tr=document.createElement('tr');build(tr,item);body.appendChild(tr)}}
async function refresh(){
 try{
  const response=await fetch('/api/v1/monitor/'+encodeURIComponent(id),{cache:'no-store'});
  if(!response.ok)throw new Error('HTTP '+response.status);
  const result=await response.json(),d=result.device;
  el('error').style.display='none';
  el('deviceTitle').textContent=[d.device_id,d.name,d.location].filter(Boolean).join(' · ');
  el('communication').textContent=d.communication;
  el('communication').className='big '+(d.communication==='ONLINE'?'ok':'bad');
  el('lastSeen').textContent='Последняя связь: '+date(d.last_seen);
  el('state').textContent=d.machine_state;
  el('state').className='big '+(d.machine_state==='ERROR'?'bad':d.machine_state==='IDLE'?'ok':'warn');
  el('activeError').textContent='Ошибка: '+d.active_error;
  el('stock').textContent=result.inventory.reduce((sum,item)=>sum+Number(item.quantity),0)+' шт.';
  el('stockDetails').textContent=result.inventory.map(i=>i.product_id+': '+i.quantity).join(' · ')||'Нет данных об остатках';
  table('sales',result.sales,4,(tr,s)=>{cell(tr,date(s.sold_at));cell(tr,s.product_id);cell(tr,s.price);cell(tr,s.session_id)});
  table('errors',result.errors,4,(tr,e)=>{cell(tr,date(e.occurred_at));cell(tr,e.error_code);cell(tr,date(e.cleared_at));cell(tr,e.cleared_at?'Закрыта':'АКТИВНА',e.cleared_at?'ok':'bad')});
  el('updated').textContent='Обновлено: '+new Date().toLocaleTimeString('ru-RU');
 }catch(err){el('error').style.display='block';el('error').textContent='Нет данных от сервера: '+err.message;el('updated').textContent='Ошибка обновления';}
}
refresh();setInterval(refresh,10000);
</script></body></html>'''


@app.get("/monitor", response_class=HTMLResponse)
def monitor_page(request: Request):
    if not _has_session(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(content=MONITOR_HTML, headers={"Cache-Control": "no-store"})
