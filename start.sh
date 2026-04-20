#!/usr/bin/env bash
# start.sh — Start all SCC200 Transport services.
# Run setup.sh first if this is a fresh clone.
#
# This script starts PostgreSQL, OTP, and the FastAPI server.
# Press Ctrl+C to stop everything.

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

# ─── Check for required local tools ──────────────────────────────────────────

TOOL_MISSING=0
for cmd in python3 java psql; do
    if ! command -v "$cmd" &>/dev/null; then
        warn "'$cmd' not found."
        TOOL_MISSING=1
    fi
done

if [[ $TOOL_MISSING -eq 1 ]]; then
    echo ""
    warn "Some local tools are missing.  Use Docker Compose instead:"
    info "  ./run.sh"
    echo ""
    info "(Only requires Docker — Python, Java, and PostgreSQL are all bundled.)"
    exit 1
fi

# ─── Activate virtual environment ────────────────────────────────────────────

VENV_DIR="$SCRIPT_DIR/.venv"
if [[ -d "$VENV_DIR" ]]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    info "Activated Python virtual environment."
else
    warn "No .venv found — using system Python.  Run setup.sh first for a clean install."
fi

# ─── Container runtime ───────────────────────────────────────────────────────

CONTAINER_CMD=""
if command -v docker &>/dev/null; then
    CONTAINER_CMD="docker"
elif command -v podman &>/dev/null; then
    CONTAINER_CMD="podman"
else
    error "Neither docker nor podman found."
    exit 1
fi

# ─── Start PostgreSQL container ──────────────────────────────────────────────

DB_CONTAINER="scc200-db"
DB_USER="transport"
DB_PASS="transport_dev"
DB_NAME="transport_db"

if $CONTAINER_CMD ps --format '{{.Names}}' 2>/dev/null | grep -qw "$DB_CONTAINER"; then
    info "PostgreSQL container already running."
else
    info "Starting PostgreSQL container …"
    $CONTAINER_CMD start "$DB_CONTAINER" 2>/dev/null || {
        error "Container '$DB_CONTAINER' does not exist.  Run setup.sh first."
        exit 1
    }
fi

# Wait for PostgreSQL
info "Waiting for PostgreSQL …"
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

# ─── Cleanup function ───────────────────────────────────────────────────────

OTP_PID=""

cleanup() {
    echo ""
    info "Shutting down …"
    if [[ -n "$OTP_PID" ]] && kill -0 "$OTP_PID" 2>/dev/null; then
        info "Stopping OTP (PID $OTP_PID) …"
        kill "$OTP_PID" 2>/dev/null || true
        wait "$OTP_PID" 2>/dev/null || true
    fi
    info "Done.  PostgreSQL container is still running — stop it with:"
    info "  $CONTAINER_CMD stop $DB_CONTAINER"
    exit 0
}
trap cleanup SIGINT SIGTERM EXIT

# ─── Start OTP ───────────────────────────────────────────────────────────────

OTP_JAR="$SCRIPT_DIR/otp-shaded-2.8.1.jar"
OTP_DATA="$SCRIPT_DIR/otp-data"
OTP_PORT="9090"

if [[ ! -f "$OTP_JAR" ]]; then
    error "OTP JAR not found at $OTP_JAR.  Run setup.sh first."
    exit 1
fi

if [[ ! -f "$OTP_DATA/graph.obj" ]]; then
    error "OTP graph not found at $OTP_DATA/graph.obj.  Run setup.sh first."
    exit 1
fi

info "Starting OpenTripPlanner on port $OTP_PORT …"
java -Xmx4G -jar "$OTP_JAR" --load "$OTP_DATA" --port "$OTP_PORT" &
OTP_PID=$!

# Wait for OTP to be ready (it can take 30-60 seconds)
info "Waiting for OTP to become ready (this may take up to 60 seconds) …"
for i in $(seq 1 90); do
    if curl -sf "http://localhost:$OTP_PORT/otp/actuators/health" &>/dev/null || \
       curl -sf "http://localhost:$OTP_PORT/otp/" &>/dev/null; then
        info "OTP is ready on port $OTP_PORT."
        break
    fi
    # Check OTP process is still alive
    if ! kill -0 "$OTP_PID" 2>/dev/null; then
        error "OTP process exited unexpectedly.  Check the output above for errors."
        exit 1
    fi
    if [[ $i -eq 90 ]]; then
        warn "OTP may still be loading — continuing anyway."
    fi
    sleep 2
done

# ─── Start the API server ───────────────────────────────────────────────────

export OTP_URL="http://localhost:$OTP_PORT"
info "OTP_URL set to $OTP_URL"

info "Starting FastAPI server on port 8080 …"
info "═══════════════════════════════════════════════════════════════"
info "  Frontend:  http://localhost:8080"
info "  API docs:  http://localhost:8080/docs"
info "  Health:    http://localhost:8080/health"
info "═══════════════════════════════════════════════════════════════"
echo ""

# Run uvicorn in the foreground — Ctrl+C triggers the cleanup trap
uvicorn app.main:app --host 0.0.0.0 --port 8080
