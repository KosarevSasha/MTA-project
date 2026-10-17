-- MTA server (SQLite3) base schema: run BEFORE mta_errors.sql, e.g.
--   sqlite3 mta.db < mta_schema.sql
-- Reconstructed from the queries in src/mta_project/mta_server.py.
-- Timestamps are ISO 8601 UTC text (see to_db_time in mta_server.py).
PRAGMA foreign_keys = ON;

BEGIN;

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    name TEXT,
    location TEXT,
    machine_state TEXT NOT NULL DEFAULT 'IDLE',
    active_error TEXT NOT NULL DEFAULT 'NONE',
    last_seen TEXT
);

CREATE TABLE IF NOT EXISTS products (
    product_id TEXT PRIMARY KEY,
    name TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    event_type TEXT NOT NULL,
    session_id TEXT,
    created_at TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(data))
);

CREATE TABLE IF NOT EXISTS inventory (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    product_id TEXT NOT NULL REFERENCES products(product_id),
    quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    PRIMARY KEY (device_id, product_id)
);

CREATE TABLE IF NOT EXISTS sales (
    sale_id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL UNIQUE,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    product_id TEXT NOT NULL REFERENCES products(product_id),
    price INTEGER NOT NULL CHECK (price > 0),
    payment TEXT NOT NULL,
    dispense TEXT NOT NULL,
    sold_at TEXT NOT NULL,
    event_id TEXT NOT NULL UNIQUE REFERENCES events(event_id)
);

CREATE INDEX IF NOT EXISTS sales_device_time ON sales (device_id, sold_at DESC);

COMMIT;
