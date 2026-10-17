"""MTA server: validation and reliability update (Python 3.8 compatible)."""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI()
logger = logging.getLogger("mta_server")


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
    if event.event_type not in ("SALE", "LOAD", "UNLOAD"):
        raise ValueError("unsupported event_type")
    if event.event_type in ("LOAD", "UNLOAD"):
        require_text(event.data.get("product_id"), "data.product_id")
        require_positive_int(event.data.get("quantity"), "data.quantity")
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
        detail={"status": "bad_request", "event_id": event_id, "reason": reason},
    )


@app.post("/api/v1/device/heartbeat")
def heartbeat(data: Heartbeat):
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
                        active_error = %s,
                        last_seen = NOW()
                    WHERE device_id = %s
                    """,
                    (data.state, data.active_error, data.device_id),
                )
                if cur.rowcount == 0:
                    raise HTTPException(status_code=404, detail="Device not found")
    except psycopg2.Error:
        logger.exception("Heartbeat database failure")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")
    return {"status": "ok"}


@app.get("/api/v1/devices/{device_id}")
def get_device(device_id: str):
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT device_id, machine_state, active_error, last_seen
                    FROM devices WHERE device_id = %s
                    """,
                    (device_id,),
                )
                row = cur.fetchone()
    except psycopg2.Error:
        logger.exception("Device lookup database failure")
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")

    if row is None:
        return {"status": "not_found", "device_id": device_id}

    found_id, machine_state, active_error, last_seen = row
    seconds_since_last_seen = None
    if last_seen is not None:
        seconds_since_last_seen = max(
            0.0, (datetime.now(timezone.utc) - last_seen).total_seconds()
        )
    communication = (
        "ONLINE"
        if seconds_since_last_seen is not None and seconds_since_last_seen <= 30
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
            if seconds_since_last_seen is not None else None
        ),
    }


@app.post("/api/v1/device/events")
def receive_event(event: DeviceEvent):
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
                        event_id, device_id, event_type, session_id,
                        created_at, data
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (event_id) DO NOTHING
                    RETURNING event_id
                    """,
                    (
                        event.event_id, event.device_id, event.event_type,
                        event.session_id, event.created_at,
                        psycopg2.extras.Json(event.data),
                    ),
                )
                if cur.fetchone() is None:
                    return {"status": "already_accepted", "event_id": event.event_id}

                if event.event_type == "SALE":
                    cur.execute(
                        """
                        INSERT INTO sales (
                            session_id, device_id, product_id, price,
                            payment, dispense, sold_at, event_id
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (
                            event.session_id, event.device_id,
                            event.data["product_id"], event.data["price"],
                            event.data["payment"], event.data["dispense"],
                            event.created_at, event.event_id,
                        ),
                    )
                    cur.execute(
                        """
                        UPDATE inventory SET quantity = quantity - 1
                        WHERE device_id = %s AND product_id = %s
                          AND quantity > 0
                        RETURNING quantity
                        """,
                        (event.device_id, event.data["product_id"]),
                    )
                    if cur.fetchone() is None:
                        raise ValueError("Product not found or inventory is empty")

                elif event.event_type == "LOAD":
                    cur.execute(
                        """
                        UPDATE inventory SET quantity = quantity + %s
                        WHERE device_id = %s AND product_id = %s
                        RETURNING quantity
                        """,
                        (
                            event.data["quantity"], event.device_id,
                            event.data["product_id"],
                        ),
                    )
                    if cur.fetchone() is None:
                        raise ValueError("Inventory record not found")

                elif event.event_type == "UNLOAD":
                    cur.execute(
                        """
                        UPDATE inventory SET quantity = quantity - %s
                        WHERE device_id = %s AND product_id = %s
                          AND quantity >= %s
                        RETURNING quantity
                        """,
                        (
                            event.data["quantity"], event.device_id,
                            event.data["product_id"], event.data["quantity"],
                        ),
                    )
                    if cur.fetchone() is None:
                        raise ValueError("Product not found or insufficient inventory")

        return {"status": "accepted", "event_id": event.event_id}

    except ValueError as error:
        raise bad_request(event.event_id, str(error))
    except psycopg2.errors.ForeignKeyViolation:
        raise bad_request(event.event_id, "Unknown device or product")
    except psycopg2.errors.UniqueViolation:
        raise bad_request(event.event_id, "Conflicting sale or session_id")
    except psycopg2.Error:
        logger.exception("Event database failure: %s", event.event_id)
        raise HTTPException(status_code=503, detail="Database temporarily unavailable")
