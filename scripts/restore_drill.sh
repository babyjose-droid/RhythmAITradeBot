#!/bin/sh
# Rhythm AI Trader — restore drill (Phase 9). Proves the latest backup can be restored.
#   PGHOST=... PGUSER=... PGPASSWORD=... BACKUP_DIR=./backups scripts/restore_drill.sh
# 1. verifies the newest dump's SHA-256;
# 2. restores it into a scratch database (DRILL_DB, default rhythm_restore_drill);
# 3. runs `rhythm-cli restore-verify` (schema at head, audit hash chain intact, row counts);
# 4. drops the scratch database. The live database is never touched.
# In Docker, where pg tools and rhythm-cli live in different images, run the restore in the
# `backup` container with VERIFY_CMD=none KEEP_DRILL_DB=1, then verify from the api container
# and drop the scratch database (see OPERATIONS_RUNBOOK.md).
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
DRILL_DB="${DRILL_DB:-rhythm_restore_drill}"
VERIFY="${VERIFY_CMD:-rhythm-cli restore-verify}"
case "$DRILL_DB" in *drill*) ;; *) echo "refusing: DRILL_DB must contain 'drill'" >&2; exit 2;; esac

latest="$(ls -1 "$BACKUP_DIR"/rhythm-*.dump 2>/dev/null | sort | tail -n 1 || true)"
[ -n "$latest" ] || { echo "no backups in $BACKUP_DIR" >&2; exit 1; }
(cd "$BACKUP_DIR" && sha256sum -c "$(basename "$latest").sha256")

dropdb --if-exists "$DRILL_DB"
createdb "$DRILL_DB"
[ -n "${KEEP_DRILL_DB:-}" ] || trap 'dropdb --if-exists "$DRILL_DB"' EXIT
pg_restore --no-owner --no-privileges --exit-on-error --dbname="$DRILL_DB" "$latest"
if [ "$VERIFY" = "none" ]; then
  echo "restored $(basename "$latest") into $DRILL_DB (not verified here; run restore-verify)"
  exit 0
fi
url="postgresql+psycopg://${PGUSER}:${PGPASSWORD}@${PGHOST:-localhost}:${PGPORT:-5432}/${DRILL_DB}"
$VERIFY --database-url "$url"
echo "restore drill OK: $(basename "$latest")"
