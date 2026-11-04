#!/bin/sh
# Rhythm AI Trader — PostgreSQL backups (Phase 9).
# Runs inside the `backup` compose service (postgres:16-alpine), or by hand:
#   PGHOST=... PGUSER=... PGPASSWORD=... PGDATABASE=... BACKUP_DIR=./backups scripts/backup.sh --once
# Writes a custom-format dump plus a SHA-256 file, then deletes dumps older than
# BACKUP_RETENTION_DAYS. Never prints credentials.
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
INTERVAL_HOURS="${BACKUP_INTERVAL_HOURS:-24}"
mkdir -p "$BACKUP_DIR"

one_backup() {
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  tmp="$BACKUP_DIR/.rhythm-$ts.dump.partial"
  out="$BACKUP_DIR/rhythm-$ts.dump"
  pg_dump --format=custom --no-owner --no-privileges --file="$tmp"
  pg_restore --list "$tmp" > /dev/null          # the archive must be readable
  mv "$tmp" "$out"                               # only complete dumps get the final name
  (cd "$BACKUP_DIR" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")
  echo "backup ok: $(basename "$out") $(wc -c < "$out") bytes"
  find "$BACKUP_DIR" -name 'rhythm-*.dump*' -type f -mtime +"$RETENTION_DAYS" -print -delete
}

if [ "${1:-}" = "--once" ]; then
  one_backup
  exit 0
fi
while true; do
  one_backup || echo "backup FAILED at $(date -u +%FT%TZ)" >&2
  sleep $((INTERVAL_HOURS * 3600))
done
