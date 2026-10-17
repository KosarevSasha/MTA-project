import os
from datetime import datetime, timezone

import psycopg2
import psycopg2.extras
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI()


class Heartbeat(BaseModel):
    device_id: str
    state: str
    active_error: str

class DeviceEvent(BaseModel):
    event_id: str
    device_id: str
    event_type: str
    session_id: str = None
    created_at: datetime
    data: dict = {}


def get_db_connection():
    return psycopg2.connect(
        host="localhost",
        port=5432,
        dbname="mta_db",
        user="postgres",
        password=os.getenv("MTA_DB_PASSWORD")
    )


@app.post("/api/v1/device/heartbeat")
def heartbeat(data: Heartbeat):

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        UPDATE devices
        SET machine_state = %s,
            active_error = %s,
            last_seen = NOW()
        WHERE device_id = %s
        """,
        (
            data.state,
            data.active_error,
            data.device_id
        )
    )

    conn.commit()

    cur.close()
    conn.close()

    print()
    print("=== HEARTBEAT ===")
    print("Device:", data.device_id)
    print("State:", data.state)
    print("Error:", data.active_error)
    print("Saved to PostgreSQL")

    return {"status": "ok"}
@app.get("/api/v1/devices/{device_id}")
def get_device(device_id: str):

    conn = get_db_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT device_id,
               machine_state,
               active_error,
               last_seen
        FROM devices
        WHERE device_id = %s
        """,
        (device_id,)
    )

    row = cur.fetchone()

    cur.close()
    conn.close()

    if row is None:
        return {
            "status": "not_found",
            "device_id": device_id
        }

    device_id, machine_state, active_error, last_seen = row

    now = datetime.now(timezone.utc)

    seconds_since_last_seen = (
        now - last_seen
    ).total_seconds()

    if seconds_since_last_seen <= 30:
        communication = "ONLINE"
    else:
        communication = "OFFLINE"

    return {
        "device_id": device_id,
        "communication": communication,
        "machine_state": machine_state,
        "active_error": active_error,
        "last_seen": last_seen,
        "seconds_since_last_seen": round(seconds_since_last_seen, 1)
    }

@app.post("/api/v1/device/events")
def receive_event(event: DeviceEvent):

    conn = get_db_connection()
    cur = conn.cursor()

    try:
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
                psycopg2.extras.Json(event.data)
            )
        )

        inserted = cur.fetchone()

        if inserted is None:
            conn.rollback()

            return {
                "status": "already_accepted",
                "event_id": event.event_id
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
                    event.event_id
                )
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
                    event.data["product_id"]
                )
            )

            new_quantity = cur.fetchone()

            if new_quantity is None:
                raise ValueError("Product not found or inventory is empty")

        if event.event_type == "LOAD":

            cur.execute(
                """
                UPDATE inventory
                SET quantity = quantity + %s
                WHERE device_id = %s
                  AND product_id = %s
                """,
                (
                    event.data["quantity"],
                    event.device_id,
                    event.data["product_id"]
                )
            )
        if event.event_type == "UNLOAD":

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
                    event.data["quantity"]
                )
            )

            new_quantity = cur.fetchone()

            if new_quantity is None:
                raise ValueError("Product not found or insufficient inventory")

        conn.commit()

        return {
            "status": "accepted",
            "event_id": event.event_id
        }

    except Exception as error:

        conn.rollback()

        print()
        print("=== EVENT ERROR ===")
        print(error)

        raise HTTPException(
            status_code=400,
            detail={
                "status": "bad_request",
                "event_id": event.event_id
            }
        )

    finally:
        cur.close()
        conn.close()