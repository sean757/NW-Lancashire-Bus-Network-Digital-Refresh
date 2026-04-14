#!/usr/bin/env bash
# setup.sh — One-time setup for the SCC200 Transport application.
# Run this ONCE on a fresh clone to install dependencies, start the database,
# load data, and prepare OTP.  After this, use start.sh to launch the app.
#
# Prerequisites: Python 3.10+, Java 21+, Docker or Podman, postgresql-client, wget
# You MUST be on the Lancaster University VPN for data ingestion to succeed.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─── Check required tools ────────────────────────────────────────────────────

check_cmd() {
    if ! command -v "$1" &>/dev/null; then
        error "'$1' is not installed.  $2"
        return 1
    fi
}

MISSING=0

check_cmd python3 "Python 3.10+"          || MISSING=1
check_cmd java    "Java 21+"              || MISSING=1
check_cmd psql    "postgresql-client"      || MISSING=1

# Accept docker or podman
CONTAINER_CMD=""
if command -v docker &>/dev/null; then
    CONTAINER_CMD="docker"
elif command -v podman &>/dev/null; then
    CONTAINER_CMD="podman"
else
    error "Neither 'docker' nor 'podman' found.  Install one of them."
    MISSING=1
fi

if [[ $MISSING -eq 1 ]]; then
    echo ""
    warn "Some local tools are missing (see above)."
    warn "You can skip local installation entirely by using Docker Compose,"
    warn "which bundles Python, Java, and PostgreSQL automatically."
    echo ""
    info "Run instead:  ./run.sh"
    echo ""
    info "This only requires Docker (with Compose v2) or podman-compose."
    exit 1
fi

info "All prerequisites found (container runtime: $CONTAINER_CMD)"

# ─── Python virtual environment ──────────────────────────────────────────────

VENV_DIR="$SCRIPT_DIR/.venv"

if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating Python virtual environment in .venv …"
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
info "Installing Python dependencies …"
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt

# ─── PostgreSQL container ────────────────────────────────────────────────────

DB_CONTAINER="scc200-db"
DB_USER="transport"
DB_PASS="transport_dev"
DB_NAME="transport_db"
DB_PORT="5432"

if $CONTAINER_CMD ps -a --format '{{.Names}}' 2>/dev/null | grep -qw "$DB_CONTAINER"; then
    info "PostgreSQL container '$DB_CONTAINER' already exists — starting it …"
    $CONTAINER_CMD start "$DB_CONTAINER" 2>/dev/null || true
else
    info "Creating PostgreSQL container '$DB_CONTAINER' …"
    $CONTAINER_CMD run -d --name "$DB_CONTAINER" \
        -e POSTGRES_USER="$DB_USER" \
        -e POSTGRES_PASSWORD="$DB_PASS" \
        -e POSTGRES_DB="$DB_NAME" \
        -p "$DB_PORT":5432 \
        postgres:16
fi

# Wait for PostgreSQL
info "Waiting for PostgreSQL to accept connections …"
for i in $(seq 1 30); do
    if PGPASSWORD="$DB_PASS" psql -h localhost -U "$DB_USER" -d "$DB_NAME" -c "SELECT 1" &>/dev/null; then
        info "PostgreSQL is ready."
        break
    fi
    if [[ $i -eq 30 ]]; then
        error "PostgreSQL did not become ready in time."
        exit 1
    fi
    sleep 1
done

# ─── Initialise database schema ─────────────────────────────────────────────

info "Initialising database schema …"
PGPASSWORD="$DB_PASS" psql -h localhost -U "$DB_USER" -d "$DB_NAME" -f scripts/init_db.sql

# ─── Data ingestion (failures are non-fatal) ────────────────────────────────

info "Running data ingestion scripts (requires University VPN) …"

run_ingest() {
    local desc="$1"; shift
    info "  → $desc …"
    if "$@"; then
        info "  ✓ $desc succeeded."
    else
        warn "  ✗ $desc failed — the app will still work with reduced data."
    fi
}

run_ingest "Ingesting stop data"       python3 -u scripts/ingest_stops.py
run_ingest "Ingesting timetable data"  python3 -u scripts/ingest_timetables.py
run_ingest "Ingesting rail data"       python3 -u scripts/ingest_rail.py

# ─── GTFS export ─────────────────────────────────────────────────────────────

info "Exporting GTFS data …"
if python3 -u scripts/export_gtfs.py gtfs_export.zip; then
    # Move into otp-data if not already there
    if [[ -f gtfs_export.zip ]]; then
        mv -f gtfs_export.zip otp-data/gtfs_export.zip
    fi
    info "GTFS export complete."
else
    warn "GTFS export failed — OTP will use the backup graph if available."
fi

# ─── OTP graph build ────────────────────────────────────────────────────────

OTP_JAR="$SCRIPT_DIR/otp-shaded-2.8.1.jar"
OTP_DATA="$SCRIPT_DIR/otp-data"

if [[ ! -f "$OTP_JAR" ]]; then
    info "Downloading OpenTripPlanner JAR …"
    wget -q --show-progress -O "$OTP_JAR" \
        "https://github.com/opentripplanner/OpenTripPlanner/releases/download/v2.8.1/otp-shaded-2.8.1.jar"
fi

OSM_FILE="$OTP_DATA/lancashire-latest.osm.pbf"
if [[ ! -f "$OSM_FILE" ]]; then
    info "Downloading Lancashire OSM extract …"
    wget -q --show-progress -O "$OSM_FILE" \
        "https://download.geofabrik.de/europe/united-kingdom/england/lancashire-latest.osm.pbf"
fi

if [[ -f "$OTP_DATA/gtfs_export.zip" && -f "$OTP_DATA/lancashire-latest.osm.pbf" ]]; then
    info "Building OTP graph (this may take a few minutes) …"
    if java -Xmx4G -jar "$OTP_JAR" --build --save "$OTP_DATA"; then
        info "OTP graph built successfully."
    else
        warn "OTP graph build failed — will attempt to use existing graph.obj if present."
    fi
else
    warn "Missing GTFS or OSM data in otp-data/ — skipping graph build."
    warn "OTP will use the backup graph.obj if available."
fi

# ─── Done ────────────────────────────────────────────────────────────────────

echo ""
info "═══════════════════════════════════════════════════════════════"
info "  Setup complete!  Run ./start.sh to launch the application."
info "═══════════════════════════════════════════════════════════════"
echo ""
