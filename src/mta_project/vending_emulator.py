"""Эмулятор торгового автомата для тестирования веб-сервиса MTA.

Только стандартная библиотека. Подкоманды:

    seed   - добавить автомат, товары и нулевые остатки в SQLite-файл
    run    - непрерывная имитация: heartbeat, продажи, загрузка, ошибки
    check  - детерминированный набор проверок API (в т.ч. негативных)

Настройка через переменные окружения или аргументы:

    MTA_URL         (по умолчанию http://127.0.0.1:8001)
    MTA_DEVICE_ID   (по умолчанию MTA-001)
    MTA_DEVICE_KEY  ключ устройства (тот же, что в MTA_DEVICE_KEYS сервера)
    MTA_DB_PATH     путь к SQLite-файлу для seed (по умолчанию mta.db)

Пример:

    uv run python -m mta_project.vending_emulator seed
    MTA_DEVICE_KEY=... uv run python -m mta_project.vending_emulator check
    MTA_DEVICE_KEY=... uv run python -m mta_project.vending_emulator run --duration 60
"""

import argparse
import json
import os
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

DEFAULT_PRODUCTS = {
    "COLA": ("Cola 0.5L", 150),
    "WATER": ("Water 0.5L", 80),
    "CHIPS": ("Chips", 120),
    "CHOCO": ("Chocolate bar", 100),
}
ERROR_CODES = ("COIN_JAM", "DISPENSER_STUCK", "COOLING_FAIL")
INITIAL_STOCK = 10


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Machine:
    """Торговый автомат: локальные остатки + клиент HTTP API сервера."""

    def __init__(self, base_url, device_id, key, timeout=5.0):
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id
        self.key = key
        self.timeout = timeout
        self.state = "IDLE"
        self.active_errors = []  # открытые коды ошибок, самая свежая последней
        self.stock = {}
        self.prices = {}

    # --- транспорт ---------------------------------------------------------

    def post(self, path, body, key=None):
        """Вернуть (http_status, json_или_текст)."""
        headers = {"Content-Type": "application/json"}
        supplied = self.key if key is None else key
        if supplied:
            headers["X-MTA-Key"] = supplied
        request = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(body).encode(),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                return resp.status, self._decode(resp.read())
        except urllib.error.HTTPError as error:
            return error.code, self._decode(error.read())

    @staticmethod
    def _decode(raw):
        text = raw.decode(errors="replace")
        try:
            return json.loads(text)
        except ValueError:
            return text

    # --- сообщения ---------------------------------------------------------

    def heartbeat(self):
        error = self.active_errors[-1] if self.active_errors else "NONE"
        return self.post("/api/v1/device/heartbeat", {
            "device_id": self.device_id,
            "state": self.state,
            "active_error": error,
        })

    def event(self, event_type, data, session_id=None, event_id=None,
              created_at=None):
        body = {
            "event_id": event_id or str(uuid.uuid4()),
            "device_id": self.device_id,
            "event_type": event_type,
            "session_id": session_id,
            "created_at": created_at or now_iso(),
            "data": data,
        }
        status, answer = self.post("/api/v1/device/events", body)
        return body, status, answer

    # --- действия автомата -------------------------------------------------

    def load(self, product_id, quantity):
        result = self.event("LOAD", {"product_id": product_id,
                                     "quantity": quantity})
        if result[1] == 200:
            self.stock[product_id] = self.stock.get(product_id, 0) + quantity
        return result

    def unload(self, product_id, quantity):
        result = self.event("UNLOAD", {"product_id": product_id,
                                       "quantity": quantity})
        if result[1] == 200:
            self.stock[product_id] -= quantity
        return result

    def sell(self, product_id):
        self.state = "SELLING"
        result = self.event("SALE", {
            "product_id": product_id,
            "price": self.prices[product_id],
            "payment": "SUCCESS",
            "dispense": "SUCCESS",
        }, session_id=str(uuid.uuid4()))
        if result[1] == 200:
            self.stock[product_id] -= 1
        self.state = "ERROR" if self.active_errors else "IDLE"
        return result

    def set_error(self, code):
        result = self.event("ERROR_SET", {"error_code": code})
        if result[1] == 200:
            self.active_errors.append(code)
            self.state = "ERROR"
        return result

    def clear_error(self, code):
        result = self.event("ERROR_CLEAR", {"error_code": code})
        if result[1] == 200:
            self.active_errors.remove(code)
            self.state = "ERROR" if self.active_errors else "IDLE"
        return result


# --- seed ------------------------------------------------------------------

def cmd_seed(args):
    conn = sqlite3.connect(args.db)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO devices (device_id, name, location)"
                " VALUES (?, ?, ?)",
                (args.device_id, "Emulated vending machine", "Test lab"),
            )
            for product_id, (name, _price) in DEFAULT_PRODUCTS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO products (product_id, name)"
                    " VALUES (?, ?)",
                    (product_id, name),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO inventory"
                    " (device_id, product_id, quantity) VALUES (?, ?, 0)",
                    (args.device_id, product_id),
                )
    finally:
        conn.close()
    print("seeded {} and {} products into {}".format(
        args.device_id, len(DEFAULT_PRODUCTS), args.db))


# --- run -------------------------------------------------------------------

def make_machine(args):
    if not args.key:
        sys.exit("device key is required: set MTA_DEVICE_KEY or pass --key")
    machine = Machine(args.url, args.device_id, args.key)
    machine.prices = {pid: price for pid, (_n, price)
                      in DEFAULT_PRODUCTS.items()}
    return machine


def log(message):
    print("{}  {}".format(datetime.now().strftime("%H:%M:%S"), message),
          flush=True)


def report(label, result):
    _body, status, answer = result
    log("{:<12} -> {} {}".format(label, status, answer))


def cmd_run(args):
    rng = random.Random(args.seed)
    machine = make_machine(args)

    status, answer = machine.heartbeat()
    log("heartbeat    -> {} {}".format(status, answer))
    if status != 200:
        sys.exit("server rejected heartbeat; check URL, key and seed")

    for product_id in DEFAULT_PRODUCTS:
        report("LOAD " + product_id, machine.load(product_id, INITIAL_STOCK))

    deadline = time.monotonic() + args.duration if args.duration else None
    next_heartbeat = 0.0
    try:
        while deadline is None or time.monotonic() < deadline:
            if time.monotonic() >= next_heartbeat:
                status, answer = machine.heartbeat()
                log("heartbeat    -> {} {} state={}".format(
                    status, answer, machine.state))
                next_heartbeat = time.monotonic() + args.heartbeat

            roll = rng.random()
            in_stock = [p for p, q in machine.stock.items() if q > 0]
            if machine.active_errors and roll < 0.3:
                code = rng.choice(machine.active_errors)
                report("ERROR_CLEAR", machine.clear_error(code))
            elif roll < args.error_rate:
                free = [c for c in ERROR_CODES if c not in machine.active_errors]
                if free:
                    report("ERROR_SET", machine.set_error(rng.choice(free)))
            elif not in_stock:
                product_id = rng.choice(list(DEFAULT_PRODUCTS))
                report("LOAD " + product_id, machine.load(product_id, INITIAL_STOCK))
            elif machine.active_errors:
                pass  # неисправный автомат ничего не продаёт
            else:
                product_id = rng.choice(in_stock)
                result = machine.sell(product_id)
                report("SALE " + product_id, result)
                if rng.random() < args.duplicate_rate:
                    body, _s, _a = result
                    again = machine.event(
                        "SALE", body["data"], session_id=body["session_id"],
                        event_id=body["event_id"], created_at=body["created_at"])
                    report("SALE retry", again)

            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("stopped")


# --- check -----------------------------------------------------------------

class Checks:
    def __init__(self):
        self.failed = 0

    def expect(self, name, actual, expected, answer=None):
        ok = actual == expected
        self.failed += not ok
        print("[{}] {}{}".format(
            "PASS" if ok else "FAIL", name,
            "" if ok else "  expected {} got {}: {}".format(
                expected, actual, answer)))


def cmd_check(args):
    machine = make_machine(args)
    check = Checks()
    product = "COLA"

    status, _ = machine.heartbeat()
    check.expect("heartbeat ok", status, 200)

    status, _ = machine.post("/api/v1/device/heartbeat", {
        "device_id": args.device_id, "state": "IDLE", "active_error": "NONE",
    }, key="")
    check.expect("heartbeat without key -> 401", status, 401)

    status, _ = machine.post("/api/v1/device/heartbeat", {
        "device_id": args.device_id, "state": "IDLE", "active_error": "NONE",
    }, key="x" * 40)
    check.expect("heartbeat wrong key -> 401", status, 401)

    _b, status, answer = machine.load(product, 3)
    check.expect("LOAD accepted", (status, answer.get("status")),
                 (200, "accepted"), answer)

    body, status, answer = machine.event("SALE", {
        "product_id": product, "price": 150,
        "payment": "SUCCESS", "dispense": "SUCCESS",
    }, session_id=str(uuid.uuid4()))
    check.expect("SALE accepted", (status, answer.get("status")),
                 (200, "accepted"), answer)

    _b, status, answer = machine.event(
        "SALE", body["data"], session_id=body["session_id"],
        event_id=body["event_id"], created_at=body["created_at"])
    check.expect("SALE retry idempotent",
                 (status, answer.get("status")),
                 (200, "already_accepted"), answer)

    _b, status, answer = machine.event("SALE", {
        "product_id": product, "price": 150,
        "payment": "SUCCESS", "dispense": "SUCCESS",
    }, session_id=body["session_id"])
    check.expect("SALE duplicate session_id -> 400", status, 400, answer)

    _b, status, answer = machine.event("SALE", {
        "product_id": product, "price": 150,
        "payment": "FAILED", "dispense": "SUCCESS",
    }, session_id=str(uuid.uuid4()))
    check.expect("SALE payment=FAILED -> 400", status, 400, answer)

    _b, status, answer = machine.event("SALE", {
        "product_id": product, "price": 0,
        "payment": "SUCCESS", "dispense": "SUCCESS",
    }, session_id=str(uuid.uuid4()))
    check.expect("SALE price=0 -> 400", status, 400, answer)

    _b, status, answer = machine.event("SALE", {
        "product_id": "NO-SUCH-PRODUCT", "price": 100,
        "payment": "SUCCESS", "dispense": "SUCCESS",
    }, session_id=str(uuid.uuid4()))
    check.expect("SALE unknown product -> 400", status, 400, answer)

    _b, status, answer = machine.event("SALE", {
        "product_id": product, "price": 150,
        "payment": "SUCCESS", "dispense": "SUCCESS",
    }, session_id=str(uuid.uuid4()), created_at="2026-01-01T00:00:00")
    check.expect("created_at without timezone -> 4xx", status in (400, 422),
                 True, answer)

    _b, status, answer = machine.event("REFUND", {})
    check.expect("unsupported event_type -> 400", status, 400, answer)

    _b, status, answer = machine.unload(product, 10_000)
    check.expect("UNLOAD more than stock -> 400", status, 400, answer)

    _b, status, answer = machine.set_error("COIN_JAM")
    check.expect("ERROR_SET accepted", status, 200, answer)
    _b, status, answer = machine.set_error("COIN_JAM")
    check.expect("ERROR_SET same code twice is a no-op (200)", status, 200, answer)
    machine.heartbeat()
    _b, status, answer = machine.clear_error("COIN_JAM")
    check.expect("ERROR_CLEAR accepted", status, 200, answer)
    _b, status, answer = machine.clear_error("COIN_JAM")
    check.expect("ERROR_CLEAR without active error -> 400", status, 400, answer)

    status, _ = machine.heartbeat()
    check.expect("heartbeat after clear", status, 200)

    machine.device_id = "NO-SUCH-DEVICE"
    status, _ = machine.heartbeat()
    check.expect("unknown device -> 401/404", status in (401, 404), True)

    print("\n{} check(s) failed".format(check.failed) if check.failed
          else "\nall checks passed")
    sys.exit(1 if check.failed else 0)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default=os.getenv("MTA_URL", "http://127.0.0.1:8001"))
    parser.add_argument("--device-id", default=os.getenv("MTA_DEVICE_ID", "MTA-001"))
    parser.add_argument("--key", default=os.getenv("MTA_DEVICE_KEY"))
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="создать автомат и товары в SQLite")
    seed.add_argument("--db", default=os.getenv("MTA_DB_PATH", "mta.db"))
    seed.set_defaults(func=cmd_seed)

    run = sub.add_parser("run", help="непрерывная имитация работы")
    run.add_argument("--duration", type=float, default=0,
                     help="секунды работы; 0 - до Ctrl+C")
    run.add_argument("--interval", type=float, default=1.0,
                     help="пауза между действиями, с")
    run.add_argument("--heartbeat", type=float, default=10.0,
                     help="период heartbeat, с")
    run.add_argument("--error-rate", type=float, default=0.05,
                     help="вероятность ошибки на шаг")
    run.add_argument("--duplicate-rate", type=float, default=0.1,
                     help="вероятность повторной отправки продажи")
    run.add_argument("--seed", type=int, default=None,
                     help="seed генератора случайных чисел")
    run.set_defaults(func=cmd_run)

    check = sub.add_parser("check", help="набор проверок API")
    check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
