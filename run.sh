#!/usr/bin/env bash
# run.sh — One-command launcher for the SCC200 Transport application.
#
# The ONLY prerequisite is Docker (with Compose v2) or Podman with podman-compose.
# Python, Java, and PostgreSQL are all provided inside the containers automatically.
#
# Usage:
#   ./run.sh              # start with data ingestion (requires University VPN)
#   ./run.sh --quick      # start using pre-built backup data only (no VPN needed)
#   ./run.sh --stop       # stop all containers
#   ./run.sh --clean      # stop and remove all containers + data volumes

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

# ─── Detect container runtime ───────────────────────────────────────────────

COMPOSE_CMD=""
if command -v podman-compose &>/dev/null; then
    # Prefer podman-compose when available — avoids issues with the
    # docker-shim on machines that have Podman but not Docker.
    COMPOSE_CMD="podman-compose"
elif command -v docker &>/dev/null && docker compose version &>/dev/null 2>&1; then
    # Only use "docker compose" when it is genuine Docker with a running daemon,
    # not the Podman shim wrapping the old Python docker-compose that needs the
    # Docker socket.  docker info failing means the daemon is not running — skip.
    if docker info &>/dev/null 2>&1 && ! docker info 2>&1 | grep -qi podman; then
        COMPOSE_CMD="docker compose"
    fi
fi

if [[ -z "$COMPOSE_CMD" ]]; then
    error "Docker (with Compose v2) or podman-compose is required."
    error "Install Docker Desktop: https://docs.docker.com/desktop/"
    exit 1
fi

info "Using: $COMPOSE_CMD"

# ─── Handle arguments ───────────────────────────────────────────────────────

ACTION="${1:-start}"

case "$ACTION" in
    --stop)
        info "Stopping containers …"
        $COMPOSE_CMD down
        info "Stopped."
        exit 0
        ;;
    --clean)
        info "Stopping containers and removing volumes …"
        $COMPOSE_CMD down -v
        info "Cleaned."
        exit 0
        ;;
    --quick)
        export SKIP_INGEST="true"
        info "Quick start — skipping data ingestion, using backup data only."
        ;;
    start|"")
        export SKIP_INGEST="false"
        info "Data ingestion enabled — ensure you are on the University VPN."
        info "(Use ./run.sh --quick to skip ingestion and use backup data only.)"
        ;;
    *)
        echo "Usage: ./run.sh [--quick | --stop | --clean]"
        exit 1
        ;;
esac

# ─── Build and start ────────────────────────────────────────────────────────

echo ""
info "═══════════════════════════════════════════════════════════════"
info "  Building and starting SCC200 Transport application …"
info "  This may take a few minutes on first run (downloading images"
info "  and building the application container)."
info "═══════════════════════════════════════════════════════════════"
echo ""
info "  Once started, open:  http://localhost:8080"
info "  API docs:            http://localhost:8080/docs"
info "  Press Ctrl+C to stop."
echo ""

$COMPOSE_CMD up --build
