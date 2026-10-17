-- MTA server v1.3: run ONCE in pgAdmin Query Tool against mta_db.
-- Existing devices/events/sales/inventory data are not modified.
BEGIN;

CREATE TABLE IF NOT EXISTS device_errors (
    error_id BIGSERIAL PRIMARY KEY,
    device_id VARCHAR(50) NOT NULL REFERENCES devices(device_id),
    error_code VARCHAR(100) NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL,
    cleared_at TIMESTAMPTZ,
    set_event_id VARCHAR(100) NOT NULL UNIQUE REFERENCES events(event_id),
    clear_event_id VARCHAR(100) UNIQUE REFERENCES events(event_id),
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT device_errors_times_valid CHECK (
        cleared_at IS NULL OR cleared_at >= occurred_at
    ),
    CONSTRAINT device_errors_clear_consistency CHECK (
        (cleared_at IS NULL) = (clear_event_id IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS device_errors_one_active_per_code
    ON device_errors (device_id, error_code)
    WHERE cleared_at IS NULL;

CREATE INDEX IF NOT EXISTS device_errors_device_timeline
    ON device_errors (device_id, occurred_at DESC);

COMMIT;
