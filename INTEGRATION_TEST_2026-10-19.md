# MTA — Integration Test Report

**Date:** 2026-10-19  
**Device:** MTA-001  
**Firmware:** v0.3.10 MVP  
**Server:** FastAPI + SQLite  
**Status:** Partial success — heartbeat working, event processing requires investigation.

## 1. Successful tests

- ESP32 successfully connected to local Wi-Fi.
- ESP32 IP: 192.168.1.104.
- Server IP: 192.168.1.106, port 8000.
- Device authentication using `X-MTA-Key` successful.
- Heartbeat returned HTTP 200 OK.
- MTA Monitor displayed ONLINE, IDLE and active error NONE.
- Physical dispensing test performed using payment emulator.

## 2. Discovered issue

After physical dispensing, the server did not register a new sale or decrease inventory.

Server logs showed repeated requests:

`POST /api/v1/device/events — HTTP 400 Bad Request`

The firmware uses product identifier `WATER_05`, while the SQLite inventory contains `WATER`.

This mismatch is a likely cause of event rejection but has not yet been confirmed from the HTTP response body.

## 3. Pending investigation

- Obtain the exact reason returned with HTTP 400.
- Confirm event payload compatibility between firmware and server.
- Determine how to process events already stored in the ESP32 NVS queue.
- Verify sale registration and inventory decrement.
- Verify recovery and duplicate-event protection after network interruption.

## 4. Development notes

Firmware changes are available in:

`ESP-MTA-001 / feature/server-auth-v0.3.10`

Server integration work is documented in:

`MTA-project / feature/esp32-integration`

No changes to the server application code were made during this integration test.

**Important:** Do not merge into `main` until the event-processing issue is resolved and integration tests are completed.
