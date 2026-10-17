"""Local-only MTA stage2 launcher with PostgreSQL password. Copy KeePassXC secrets on request; never paste into CMD.
Do not use for internet-facing deployment. Requires: pip install python-multipart
"""
import hashlib
import json
import os
import secrets
import sys


def clipboard():
    import tkinter
    root = tkinter.Tk()
    root.withdraw()
    try:
        root.update()
        return root.clipboard_get()
    finally:
        root.destroy()


def request_secret(label, min_length):
    print('\nCopy PASSWORD from KeePassXC entry: {}'.format(label))
    input('Return to this window and press Enter ONLY (no Ctrl+V): ')
    secret = clipboard()
    if len(secret) < min_length:
        raise ValueError('Secret too short ({} characters, need at least {}).'.format(len(secret), min_length))
    print('Secret received ({} characters); not displayed.'.format(len(secret)))
    return secret


def main():
    print('MTA server 1.5 stage 2 - local operator login test')
    try:
        device_key = request_secret('MTA-001 API Key', 32)
        os.environ['MTA_DEVICE_KEYS'] = json.dumps({'MTA-001': device_key})
        del device_key
        admin_password = request_secret('MTA Monitor Admin', 20)
        salt = secrets.token_bytes(16)
        rounds = 310000
        digest = hashlib.pbkdf2_hmac('sha256', admin_password.encode('utf-8'), salt, rounds)
        os.environ['MTA_ADMIN_PASSWORD_HASH'] = 'pbkdf2_sha256${}${}${}'.format(rounds, salt.hex(), digest.hex())
        del admin_password
        db_password = request_secret('MTA PostgreSQL', 1)
        os.environ['MTA_DB_PASSWORD'] = db_password
        del db_password
        os.environ['MTA_SESSION_SECRET'] = secrets.token_urlsafe(48)
        os.environ['MTA_HTTPS'] = '0'  # local 127.0.0.1 ONLY; use HTTPS and secure cookies on VPS
        print('\nConfiguration ready. Local server: http://127.0.0.1:8001/login')
        import uvicorn
        uvicorn.run('mta_server_v1_5_stage2:app', host='127.0.0.1', port=8001)
    except (EOFError, KeyboardInterrupt):
        print('\nCancelled.')
        return 1
    except Exception as exc:
        print('Could not start: {}'.format(exc))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
