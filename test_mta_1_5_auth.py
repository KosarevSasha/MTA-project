"""Safe local MTA v1.5 authentication test. Does not write to database."""
import getpass
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

URL = 'http://127.0.0.1:8001/api/v1/device/events'


def probe(key):
    # A deliberately unsupported event type: validation must reject it before DB access.
    payload = {
        'event_id': 'MTA-001-AUTH-TEST-NOT-A-REAL-EVENT',
        'device_id': 'MTA-001',
        'event_type': 'AUTH_TEST_INVALID',
        'session_id': None,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'data': {},
    }
    request = urllib.request.Request(
        URL,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'X-MTA-Key': key},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return response.status, response.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode('utf-8', 'replace')


def main():
    print('MTA server 1.5 - safe API authentication check')
    print('Server must already be running on 127.0.0.1:8001.')
    try:
        key = getpass.getpass('Paste MTA-001 key from KeePassXC: ')
    except (EOFError, KeyboardInterrupt):
        print('\nCancelled.')
        return 1
    if len(key) < 32:
        print('Key is shorter than 32 characters; try again.')
        return 1
    try:
        bad_status, _ = probe('not-the-correct-key')
        good_status, good_body = probe(key)
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        print('Connection error:', str(error))
        return 1
    finally:
        del key
    print('Wrong key: HTTP', bad_status, '(expected 401)')
    print('Your key:  HTTP', good_status, '(expected 400)')
    if bad_status == 401 and good_status == 400 and 'unsupported event_type' in good_body:
        print('PASS: authorization succeeded; deliberately invalid event was rejected.')
        print('No sales, inventory or device states were changed by this test.')
        return 0
    print('CHECK NEEDED: unexpected response. Do not share any secret keys.')
    print('Response to valid-key test:', good_body[:350])
    return 1


if __name__ == '__main__':
    sys.exit(main())
