#!/usr/bin/env bash
# docker-start.sh — Entrypoint for the Docker Compose app container.
# Waits for PostgreSQL, initialises the schema, optionally ingests data,
# starts OTP in the background, then starts the FastAPI server.

set -euo pipefail

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-5432}"
DB_USER="${DB_USER:-transport}"
DB_PASS="${DB_PASSWORD:-transport_dev}"
DB_NAME="${DB_NAME:-transport_db}"
OTP_PORT="9090"
SKIP_INGEST="${SKIP_INGEST:-true}"

# ─── Wait for PostgreSQL ────────────────────────────────────────────────────

info "Waiting for PostgreSQL at $DB_HOST:$DB_PORT …"
for i in $(seq 1 60); do
    if PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1" &>/dev/null; then
        info "PostgreSQL is ready."
        break
    fi
    if [[ $i -eq 60 ]]; then
        error "PostgreSQL did not become ready."
        exit 1
    fi
    sleep 1
done

# ─── Initialise schema ──────────────────────────────────────────────────────

info "Initialising database schema …"
PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -f scripts/init_db.sql

# ─── Optional data ingestion ────────────────────────────────────────────────

if [[ "$SKIP_INGEST" != "true" ]]; then
    info "Running data ingestion (SKIP_INGEST != true) …"
    python3 -u scripts/ingest_stops.py      || warn "Stop ingestion failed."
    python3 -u scripts/ingest_timetables.py || warn "Timetable ingestion failed."
    python3 -u scripts/ingest_rail.py       || warn "Rail ingestion failed."
    python3 -u scripts/export_gtfs.py gtfs_export.zip && \
        mv -f gtfs_export.zip otp-data/gtfs_export.zip || warn "GTFS export failed."
else
    info "Skipping data ingestion (using pre-built data).  Set SKIP_INGEST=false to ingest."
    # Restore from committed database snapshot if available
    if [[ -f /workspace/db-snapshot/transport_db.sql.gz ]]; then
        info "Restoring database from snapshot …"
        gunzip -c /workspace/db-snapshot/transport_db.sql.gz \
            | PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" --quiet --single-transaction \
            && info "Database restored from snapshot." \
            || warn "Snapshot restore failed — database may be empty."
    else
        warn "No database snapshot found at db-snapshot/transport_db.sql.gz"
    fi
fi

# ─── Build OTP graph if needed ───────────────────────────────────────────────

OTP_JAR="/workspace/otp-shaded-2.8.1.jar"
OTP_DATA="/workspace/otp-data"

if [[ ! -f "$OTP_JAR" ]]; then
    info "Downloading OpenTripPlanner JAR …"
    wget -q --show-progress -O "$OTP_JAR" \
        "https://github.com/opentripplanner/OpenTripPlanner/releases/download/v2.8.1/otp-shaded-2.8.1.jar"
fi

OSM_FILE="$OTP_DATA/lancashire-latest.osm.pbf"
if [[ ! -f "$OSM_FILE" ]]; then
    info "Downloading Lancashire OSM extract …"
    wget -q -O "$OSM_FILE" \
        "https://download.geofabrik.de/europe/united-kingdom/england/lancashire-latest.osm.pbf"
fi

if [[ -f "$OTP_JAR" ]]; then
    # Rebuild graph when GTFS data is newer than the existing graph (or no graph exists)
    if [[ ! -f "$OTP_DATA/graph.obj" ]] || \
       [[ -f "$OTP_DATA/gtfs_export.zip" && "$OTP_DATA/gtfs_export.zip" -nt "$OTP_DATA/graph.obj" ]]; then
        info "Building OTP graph from GTFS + OSM data …"
        java -Xmx4G -jar "$OTP_JAR" --build --save "$OTP_DATA"
        info "OTP graph build complete."
    else
        info "OTP graph is up-to-date — skipping build."
    fi
fi

# ─── Start OTP ───────────────────────────────────────────────────────────────

if [[ -f "$OTP_JAR" && -f "$OTP_DATA/graph.obj" ]]; then
    info "Starting OpenTripPlanner on port $OTP_PORT …"
    java -Xmx4G -jar "$OTP_JAR" --load "$OTP_DATA" --port "$OTP_PORT" &
    OTP_PID=$!

    info "Waiting for OTP to become ready …"
    for i in $(seq 1 90); do
        if curl -sf "http://localhost:$OTP_PORT/otp/actuators/health" &>/dev/null || \
           curl -sf "http://localhost:$OTP_PORT/otp/" &>/dev/null; then
            info "OTP is ready."
            break
        fi
        if ! kill -0 "$OTP_PID" 2>/dev/null; then
            warn "OTP exited unexpectedly — journey planning will not work."
            break
        fi
        if [[ $i -eq 90 ]]; then
            warn "OTP may still be loading — continuing anyway."
        fi
        sleep 2
    done
    export OTP_URL="http://localhost:$OTP_PORT"
else
    warn "OTP JAR or graph not found — journey planning will not work."
fi

# ─── Start FastAPI ───────────────────────────────────────────────────────────

info "Starting FastAPI server on port 8080 …"
info "═══════════════════════════════════════════════════════════════"
info "  Frontend:  http://localhost:8080"
info "  API docs:  http://localhost:8080/docs"
info "  Health:    http://localhost:8080/health"
info "═══════════════════════════════════════════════════════════════"

exec uvicorn app.main:app --host 0.0.0.0 --port 8080
