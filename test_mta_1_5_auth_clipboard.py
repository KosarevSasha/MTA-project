"""Local-only MTA 1.5 auth test. Reads key from Windows clipboard without echoing it."""
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

URL = 'http://127.0.0.1:8001/api/v1/device/events'


def read_clipboard():
    import tkinter
    root = tkinter.Tk()
    root.withdraw()
    try:
        root.update()
        return root.clipboard_get()
    finally:
        root.destroy()


def probe(key):
    payload = {
        'event_id': 'MTA-001-AUTH-TEST-NOT-A-REAL-EVENT',
        'device_id': 'MTA-001',
        'event_type': 'AUTH_TEST_INVALID',
        'session_id': None,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'data': {},
    }
    req = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'X-MTA-Key': key},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            return response.status, response.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode('utf-8', 'replace')


def main():
    print('MTA 1.5 - local authentication test (clipboard method)')
    print('Ensure test server is running on 127.0.0.1:8001.')
    print('Copy the PASSWORD from the MTA-001 API Key entry in KeePassXC.')
    try:
        input('Return here and press Enter within the clipboard timeout... ')
        key = read_clipboard()
    except (KeyboardInterrupt, EOFError):
        print('\nCancelled.')
        return 1
    except Exception:
        print('Clipboard is empty or does not contain text. Copy the password again.')
        return 1
    if len(key) != 48:
        print('Received', len(key), 'characters; expected 48. No request sent.')
        return 1
    try:
        wrong_status, _ = probe('incorrect-test-key')
        correct_status, correct_body = probe(key)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        print('Connection error:', str(error))
        return 1
    finally:
        del key
    print('Wrong key: HTTP', wrong_status, '(expected 401)')
    print('Your key:  HTTP', correct_status, '(expected 400)')
    if wrong_status == 401 and correct_status == 400 and 'unsupported event_type' in correct_body:
        print('PASS: key accepted, invalid test event rejected; database unchanged.')
        return 0
    print('CHECK NEEDED: Unexpected response; do not share any secret.')
    print('Response:', correct_body[:200])
    return 1


if __name__ == '__main__':
    sys.exit(main())
