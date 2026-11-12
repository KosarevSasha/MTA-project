# MTA Server — VPS Deployment

## Environment

- OS: Ubuntu 24.04 LTS
- Application: FastAPI + SQLite
- Python: 3.14
- Package manager: uv
- Service manager: systemd
- Application user: mtaadmin

## Paths

- Repository: /home/mtaadmin/mta-server
- Database: /var/lib/mta/mta.db
- Configuration: /etc/mta/mta.env
- Service: /etc/systemd/system/mta-server.service

## Network

- Listen address: 127.0.0.1
- Port: 8001
- Public access: disabled
- Operator access: SSH tunnel

## Security

- Secrets are stored outside the Git repository.
- The production SQLite database is stored outside Git.
- The server runs as the unprivileged user mtaadmin.
- Do not commit API keys, passwords or session secrets.

## Current status

Initial VPS deployment completed on 2026-11-12.
Service is running under systemd.
Reboot recovery has not yet been tested.
ESP32 integration is pending.

## Installation and service management

Install project dependencies:

    uv sync --locked --no-default-groups

Install or update systemd unit:

    sudo install -o root -g root -m 644 deploy/mta-server.service /etc/systemd/system/mta-server.service
    sudo systemctl daemon-reload

Enable automatic startup:

    sudo systemctl enable mta-server.service

Start or restart the application:

    sudo systemctl start mta-server.service
    sudo systemctl restart mta-server.service

Check service status:

    systemctl status mta-server.service --no-pager

View recent logs:

    journalctl -u mta-server.service -n 50 --no-pager

Check local HTTP endpoint:

    curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:8001/login

## Updating the application

1. Coordinate changes with the project maintainer.
2. Review the Git branch and pending changes.
3. Back up the production database and configuration securely.
4. Update the approved application revision through Git.
5. Run uv sync --locked --no-default-groups.
6. Run tests and review database migration requirements.
7. Restart mta-server.service.
8. Verify service logs, HTTP response and MTA Monitor.

Do not overwrite the production database with the repository test database.
Do not apply SQL schemas as migrations without reviewing their effects.
Do not automatically merge deployment changes into main.

## Backup

Back up /var/lib/mta/mta.db using the SQLite backup API
or the sqlite3 .backup command, not a plain file copy while
the application is running in WAL mode.

Store the backup of /etc/mta/mta.env securely, with restricted
access. Never place it in Git.

A tested restore procedure must be prepared before production use.
