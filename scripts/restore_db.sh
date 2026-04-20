#!/usr/bin/env bash
# restore_db.sh — Restore the database from a committed snapshot.
#
# Usage:  bash scripts/restore_db.sh [host]
#   host defaults to "localhost" for local dev, use "db" inside Docker.

set -euo pipefail

DB_HOST="${1:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-transport}"
DB_NAME="${DB_NAME:-transport_db}"
PGPASSWORD="${DB_PASSWORD:-transport_dev}"
export PGPASSWORD

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SNAPSHOT_FILE="$SCRIPT_DIR/../db-snapshot/transport_db.sql.gz"

if [[ ! -f "$SNAPSHOT_FILE" ]]; then
    echo "[ERROR] Snapshot not found at db-snapshot/transport_db.sql.gz"
    echo "[ERROR] Run 'bash scripts/snapshot_db.sh' first to create one."
    exit 1
fi

echo "[INFO] Restoring database '$DB_NAME' from snapshot …"

if command -v psql &>/dev/null; then
    gunzip -c "$SNAPSHOT_FILE" | psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" --quiet --single-transaction
elif command -v podman &>/dev/null; then
    gunzip -c "$SNAPSHOT_FILE" | podman exec -i -e PGPASSWORD="$PGPASSWORD" scc200-db \
        psql -h localhost -U "$DB_USER" -d "$DB_NAME" --quiet --single-transaction
elif command -v docker &>/dev/null; then
    gunzip -c "$SNAPSHOT_FILE" | docker exec -i -e PGPASSWORD="$PGPASSWORD" scc200-db \
        psql -h localhost -U "$DB_USER" -d "$DB_NAME" --quiet --single-transaction
else
    echo "[ERROR] Neither psql, podman, nor docker found."
    exit 1
fi

echo "[INFO] Database restored successfully from snapshot."
