"""Виртуальный торговый автомат для тестов и интерактивный эмулятор.

Класс VirtualMTA используется в pytest-тестах; запуск как скрипта даёт
интерактивный эмулятор.

Подкоманды:

    seed   - добавить автомат, товары и нулевые остатки в SQLite-файл
    run    - heartbeat по таймеру; клавиши 1-9 генерируют события

Настройка через переменные окружения или аргументы:

    MTA_URL         (по умолчанию http://127.0.0.1:8001)
    MTA_DEVICE_ID   (по умолчанию MTA-001)
    MTA_DEVICE_KEY  ключ устройства (тот же, что в MTA_DEVICE_KEYS сервера)
    MTA_DB_PATH     путь к SQLite-файлу для seed (по умолчанию mta.db)

Пример:

    uv run python tests/virtualmta.py seed
    MTA_DEVICE_KEY=... uv run python tests/virtualmta.py run --heartbeat 5
"""

import argparse
import enum
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from typing import Any


class MTAState(enum.StrEnum):
    IDLE = enum.auto()
    WAIT_PAYMENT = enum.auto()
    DISPENSING = enum.auto()
    SUCCESS = enum.auto()
    ERROR = enum.auto()


class VirtualMTA:
    def __init__(
        self,
        base_url: str,
        device_id: str,
        key: str,
        *,
        timeout: float = 5.0,
        state: MTAState = MTAState.IDLE,
        active_errors: list[str] | None = None,
        prices: dict[int, float] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.device_id = device_id
        self.key = key
        self.timeout = timeout
        self.state = state
        self.active_errors = active_errors or []
        self.prices = prices or {}

        self.product_id: str | None = None
        self.session_id: str | None = None
        self.payment_ok = False

    # --- транспорт ---------------------------------------------------------

    def post(self, path: str, body: dict, key: str | None = None) -> tuple[int, Any]:
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
    def _decode(raw: bytes) -> Any:
        text = raw.decode(errors="replace")
        try:
            return json.loads(text)
        except ValueError:
            return text

    # --- сообщения ---------------------------------------------------------

    def heartbeat(self) -> tuple[int, Any]:
        error = self.active_errors[-1] if self.active_errors else "NONE"
        return self.post(
            "/api/v1/device/heartbeat",
            {
                "device_id": self.device_id,
                "state": self.state.upper(),
                "active_error": error,
            },
        )

    def event(
        self,
        event_type: str,
        data: dict,
        session_id: str | None = None,
        event_id: str | None = None,
        created_at: str | None = None,
    ) -> tuple[dict, int, Any]:
        body = {
            "event_id": event_id or str(uuid.uuid4()),
            "device_id": self.device_id,
            "event_type": event_type,
            "session_id": session_id,
            "created_at": created_at or datetime.now(timezone.utc).isoformat(),
            "data": data,
        }
        status, answer = self.post("/api/v1/device/events", body)
        return body, status, answer

    # --- складские события -------------------------------------------------

    def load(self, product_id: str, quantity: int) -> tuple[dict, int, Any]:
        return self.event("LOAD", {"product_id": product_id, "quantity": quantity})

    def unload(self, product_id: str, quantity: int) -> tuple[dict, int, Any]:
        return self.event("UNLOAD", {"product_id": product_id, "quantity": quantity})

    # --- ошибки ------------------------------------------------------------

    def set_error(self, code: str) -> tuple[dict, int, Any]:
        result = self.event("ERROR_SET", {"error_code": code})
        if result[1] == 200:
            if code not in self.active_errors:
                self.active_errors.append(code)
            self.state = MTAState.ERROR
        return result

    def clear_error(self, code: str) -> tuple[dict, int, Any]:
        result = self.event("ERROR_CLEAR", {"error_code": code})
        if result[1] == 200:
            if code in self.active_errors:
                self.active_errors.remove(code)
            if not self.active_errors:
                self.state = MTAState.IDLE
        return result

    # --- сценарий продажи --------------------------------------------------
    # IDLE -> WAIT_PAYMENT -> DISPENSING -> SUCCESS -> IDLE

    def _require_state(self, *allowed: MTAState) -> None:
        if self.state not in allowed:
            raise RuntimeError(
                f"invalid state {self.state}, expected one of {[str(s) for s in allowed]}"
            )

    def start_sale(self, product_id: str) -> None:
        """Клиент выбрал товар: ждём оплату."""
        self._require_state(MTAState.IDLE)
        if product_id not in self.prices:
            raise KeyError(f"no price for product {product_id!r}")
        self.product_id = product_id
        self.session_id = str(uuid.uuid4())
        self.state = MTAState.WAIT_PAYMENT

    def pay(self, *, success: bool = True) -> None:
        """Результат оплаты. Неуспешная оплата отменяет сессию без события."""
        self._require_state(MTAState.WAIT_PAYMENT)
        self.payment_ok = success
        if success:
            self.state = MTAState.DISPENSING
        else:
            self._cancel_session()

    def dispense(self, *, success: bool = True) -> tuple[dict, int, Any] | None:
        """Выдача товара; при успехе отправляет SALE и возвращает (body, status, answer)."""
        if (price := self.prices.get(self.product_id)) is None:
            raise RuntimeError(
                f"Unexpected {price=} ({self.product_id=}, {self.prices=})"
            )

        self._require_state(MTAState.DISPENSING)
        if not success:
            self._reset_session()
            self.state = MTAState.ERROR
            return None
        self.state = MTAState.SUCCESS

        result = self.event(
            "SALE",
            {
                "product_id": self.product_id,
                "price": price,
                "payment": "SUCCESS",
                "dispense": "SUCCESS",
            },
            session_id=self.session_id,
        )
        self._reset_session()
        return result

    def finish(self) -> None:
        """Вернуться из SUCCESS в IDLE (или ERROR, если есть открытые ошибки)."""
        self._require_state(MTAState.SUCCESS)
        self.state = MTAState.ERROR if self.active_errors else MTAState.IDLE

    def sell(self, product_id: str) -> tuple[dict, int, Any]:
        """Полная успешная продажа за один вызов."""
        self.start_sale(product_id)
        self.pay()
        result = self.dispense()
        self.finish()
        assert result is not None
        return result

    def _reset_session(self) -> None:
        self.product_id = None
        self.session_id = None
        self.payment_ok = False

    def _cancel_session(self) -> None:
        self._reset_session()
        self.state = MTAState.ERROR if self.active_errors else MTAState.IDLE


# --- интерактивный эмулятор ---------------------------------------------

DEFAULT_PRODUCTS = {
    "COLA": ("Cola 0.5L", 150),
    "WATER": ("Water 0.5L", 80),
    "CHIPS": ("Chips", 120),
    "CHOCO": ("Chocolate bar", 100),
}
ERROR_CODES = ("COIN_JAM", "DISPENSER_STUCK", "COOLING_FAIL")
LOAD_QUANTITY = 10


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
                    "INSERT OR IGNORE INTO products (product_id, name) VALUES (?, ?)",
                    (product_id, name),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO inventory"
                    " (device_id, product_id, quantity) VALUES (?, ?, 0)",
                    (args.device_id, product_id),
                )
    finally:
        conn.close()
    print(
        "seeded {} and {} products into {}".format(
            args.device_id, len(DEFAULT_PRODUCTS), args.db
        )
    )


# --- run -------------------------------------------------------------------


def make_machine(args):
    if not args.key:
        sys.exit("device key is required: set MTA_DEVICE_KEY or pass --key")
    prices = {pid: price for pid, (_n, price) in DEFAULT_PRODUCTS.items()}
    return VirtualMTA(args.url, args.device_id, args.key, prices=prices)


def log(message):
    print("{}  {}".format(datetime.now().strftime("%H:%M:%S"), message), flush=True)


def report(label, result):
    _body, status, answer = result
    log("{:<12} -> {} {}".format(label, status, answer))


PRODUCT_KEYS = dict(zip("1234", DEFAULT_PRODUCTS))
HELP = """\
клавиши (Enter не нужен):
  1-4  SALE: COLA, WATER, CHIPS, CHOCO
  5    LOAD: +{} шт. каждого товара
  6    повтор последней продажи (тот же event_id)
  7    ERROR_SET: следующий код из {}
  8    ERROR_CLEAR: снять последнюю ошибку
  9    UNLOAD: -1 шт. каждого товара
  h    эта справка, q - выход""".format(LOAD_QUANTITY, ", ".join(ERROR_CODES))


def handle_key(machine, key, last_sale):
    """Выполнить действие клавиши; вернуть тело последней продажи."""
    if key in PRODUCT_KEYS:
        try:
            result = machine.sell(PRODUCT_KEYS[key])
        except RuntimeError as error:
            log("продажа невозможна: {}".format(error))
            return last_sale
        report("SALE " + PRODUCT_KEYS[key], result)
        return result[0] if result[1] == 200 else last_sale
    if key == "5":
        for product_id in DEFAULT_PRODUCTS:
            report("LOAD " + product_id, machine.load(product_id, LOAD_QUANTITY))
    elif key == "6":
        if last_sale is None:
            log("нет продажи для повтора")
        else:
            report(
                "SALE retry",
                machine.event(
                    "SALE",
                    last_sale["data"],
                    session_id=last_sale["session_id"],
                    event_id=last_sale["event_id"],
                    created_at=last_sale["created_at"],
                ),
            )
    elif key == "7":
        free = [c for c in ERROR_CODES if c not in machine.active_errors]
        if free:
            report("ERROR_SET " + free[0], machine.set_error(free[0]))
        else:
            log("все коды ошибок уже активны")
    elif key == "8":
        if machine.active_errors:
            code = machine.active_errors[-1]
            report("ERROR_CLEAR " + code, machine.clear_error(code))
        else:
            log("активных ошибок нет")
    elif key == "9":
        for product_id in DEFAULT_PRODUCTS:
            report("UNLOAD " + product_id, machine.unload(product_id, 1))
    return last_sale


def cmd_run(args):
    import select
    import termios
    import tty

    machine = make_machine(args)
    interactive = sys.stdin.isatty()
    saved = termios.tcgetattr(sys.stdin) if interactive else None
    last_sale = None
    next_heartbeat = 0.0
    print(HELP)
    try:
        if interactive:
            tty.setcbreak(sys.stdin.fileno())
        while True:
            if time.monotonic() >= next_heartbeat:
                status, answer = machine.heartbeat()
                log(
                    "heartbeat    -> {} {} state={}".format(
                        status, answer, machine.state
                    )
                )
                next_heartbeat = time.monotonic() + args.heartbeat
            wait = max(0.0, next_heartbeat - time.monotonic())
            if not select.select([sys.stdin], [], [], wait)[0]:
                continue
            key = os.read(sys.stdin.fileno(), 1).decode(errors="ignore")
            if key in ("q", "\x03", ""):  # q, Ctrl+C, EOF
                break
            if key == "h":
                print(HELP)
            else:
                last_sale = handle_key(machine, key, last_sale) or last_sale
    except KeyboardInterrupt:
        pass
    finally:
        if interactive:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, saved)
    log("stopped")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--url", default=os.getenv("MTA_URL", "http://127.0.0.1:8001"))
    parser.add_argument("--device-id", default=os.getenv("MTA_DEVICE_ID", "MTA-001"))
    parser.add_argument("--key", default=os.getenv("MTA_DEVICE_KEY"))
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="создать автомат и товары в SQLite")
    seed.add_argument("--db", default=os.getenv("MTA_DB_PATH", "mta.db"))
    seed.set_defaults(func=cmd_seed)

    run = sub.add_parser("run", help="heartbeat по таймеру; клавиши 1-9 шлют события")
    run.add_argument(
        "--heartbeat", type=float, default=10.0, help="период heartbeat, с"
    )
    run.set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
