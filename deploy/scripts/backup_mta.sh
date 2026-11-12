#!/usr/bin/env bash
set -Eeuo pipefail
trap 'rc=$?; echo "ERROR: backup script failed at line $LINENO (exit $rc)" >&2' ERR
umask 077
LOCK="/var/lib/mta/backups/.backup.lock"
exec 9>"$LOCK"
flock -n 9 || { echo "Backup already running" >&2; exit 1; }

DB="/var/lib/mta/mta.db"
BACKUP_DIR="/var/lib/mta/backups"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
FINAL="$BACKUP_DIR/mta-$STAMP.db"

mkdir -p "$BACKUP_DIR"

TMP="$(mktemp "$BACKUP_DIR/.mta-XXXXXXXX.db")"
trap 'rm -f -- "$TMP"' EXIT

echo "Starting MTA backup: $STAMP"

sqlite3 "$DB" ".backup '$TMP'"

RESULT="$(sqlite3 "$TMP" 'PRAGMA integrity_check;')"
if [[ "$RESULT" != "ok" ]]; then
    echo "ERROR: SQLite integrity check failed" >&2
    exit 1
fi

FK_ERRORS="$(sqlite3 "$TMP" 'PRAGMA foreign_key_check;')"
if [[ -n "$FK_ERRORS" ]]; then
    echo "ERROR: Foreign key check failed" >&2
    exit 1
fi

ln -- "$TMP" "$FINAL" && rm -- "$TMP"
trap - EXIT

echo "Backup completed: $FINAL"

# Retain the seven most recent timestamped backups.
mapfile -d '' -t BACKUPS < <(
    find "$BACKUP_DIR" -maxdepth 1 -type f \
        -regextype posix-extended \
        -regex '.*/mta-[0-9]{8}T[0-9]{6}Z\.db' \
        -print0 | sort -zr
)

if (( ${#BACKUPS[@]} > 7 )); then
    for OLD in "${BACKUPS[@]:7}"; do
        rm -- "$OLD"
        echo "Removed old backup: $OLD"
    done
fi

echo "Backup rotation completed"
