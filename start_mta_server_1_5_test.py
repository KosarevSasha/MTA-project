"""Local test launcher for MTA server v1.5 stage 1 (Windows, Python 3.8+).

Keeps the device secret out of command-line arguments and command history.
The secret exists in this process's memory/environment while the server runs.
"""
import getpass
import json
import os
import sys


def main():
    print('MTA server 1.5 - local authentication test')
    print('Paste the MTA-001 API key from KeePassXC and press Enter.')
    print('Input is hidden; no characters will appear.')
    try:
        key = getpass.getpass('MTA-001 API key: ')
    except (EOFError, KeyboardInterrupt):
        print('\nCancelled.')
        return 1

    if len(key) < 32:
        print('Error: key is too short (minimum 32 characters).')
        return 1

    os.environ['MTA_DEVICE_KEYS'] = json.dumps({'MTA-001': key})
    # Drop the local reference; the server reads the process environment.
    del key
    print('Key configuration accepted. Starting server on 127.0.0.1:8001 ...')
    import uvicorn
    uvicorn.run('mta_server_v1_5_stage1:app', host='127.0.0.1', port=8001)
    return 0


if __name__ == '__main__':
    sys.exit(main())
