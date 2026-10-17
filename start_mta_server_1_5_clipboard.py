"""Local Windows test launcher. Reads device key from clipboard, never prints it.
Only for local development on 127.0.0.1:8001; not a production secret store.
"""
import json
import os
import sys


def read_clipboard():
    import tkinter
    root = tkinter.Tk()
    root.withdraw()
    try:
        root.update()
        return root.clipboard_get()
    finally:
        root.destroy()


def main():
    print('MTA 1.5 - local test server (clipboard key)')
    print('Copy PASSWORD from KeePassXC entry MTA-001 API Key.')
    try:
        input('Return here and press Enter within clipboard timeout... ')
        key = read_clipboard()
    except (EOFError, KeyboardInterrupt):
        print('\nCancelled.')
        return 1
    except Exception:
        print('Clipboard unavailable or contains no text. Nothing started.')
        return 1
    if len(key) != 48:
        print('Received {} characters; expected 48. Nothing started.'.format(len(key)))
        return 1
    os.environ['MTA_DEVICE_KEYS'] = json.dumps({'MTA-001': key})
    del key
    print('Key length OK (48). Starting local server on 127.0.0.1:8001.')
    import uvicorn
    uvicorn.run('mta_server_v1_5_stage1:app', host='127.0.0.1', port=8001)
    return 0


if __name__ == '__main__':
    sys.exit(main())
