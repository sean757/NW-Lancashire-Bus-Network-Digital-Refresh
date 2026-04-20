#!/usr/bin/env bash
# snapshot_db.sh — Dump the current PostgreSQL database to a SQL file
# that can be committed to the repository as a fallback snapshot.
#
# Usage:  bash scripts/snapshot_db.sh [host]
#   host defaults to "localhost" for local dev, use "db" inside Docker.

set -euo pipefail

DB_HOST="${1:-localhost}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-transport}"
DB_NAME="${DB_NAME:-transport_db}"
PGPASSWORD="${DB_PASSWORD:-transport_dev}"
export PGPASSWORD

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SNAPSHOT_DIR="$SCRIPT_DIR/../db-snapshot"
SNAPSHOT_FILE="$SNAPSHOT_DIR/transport_db.sql.gz"

mkdir -p "$SNAPSHOT_DIR"

echo "[INFO] Dumping database '$DB_NAME' from $DB_HOST:$DB_PORT …"

# Try local pg_dump first; fall back to running it inside the container
if command -v pg_dump &>/dev/null; then
    pg_dump -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" \
        --no-owner --no-privileges --clean --if-exists \
        | gzip > "$SNAPSHOT_FILE"
elif command -v podman &>/dev/null; then
    podman exec -e PGPASSWORD="$PGPASSWORD" scc200-db \
        pg_dump -h localhost -U "$DB_USER" -d "$DB_NAME" \
        --no-owner --no-privileges --clean --if-exists \
        | gzip > "$SNAPSHOT_FILE"
elif command -v docker &>/dev/null; then
    docker exec -e PGPASSWORD="$PGPASSWORD" scc200-db \
        pg_dump -h localhost -U "$DB_USER" -d "$DB_NAME" \
        --no-owner --no-privileges --clean --if-exists \
        | gzip > "$SNAPSHOT_FILE"
else
    echo "[ERROR] Neither pg_dump, podman, nor docker found."
    exit 1
fi

SIZE=$(du -h "$SNAPSHOT_FILE" | cut -f1)
echo "[INFO] Snapshot saved to db-snapshot/transport_db.sql.gz ($SIZE)"
echo "[INFO] Commit this file to the repository for offline testing."
