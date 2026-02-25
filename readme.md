# SCC200 Transport - Developer Setup

## Prerequisites

- **VS Code** with the [Dev Containers extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-containers) installed
- **Podman** (available on university lab machines) or **Docker**
- **Git**

## Setup Steps

### 1. Clone the repository

```bash
git clone https://github.com/sean757/NW-Lancashire-Bus-Network-Digital-Refresh.git
cd NW-Lancashire-Bus-Network-Digital-Refresh
```

### 2. Start the PostgreSQL database

Run this on the **host machine** (outside of the devcontainer):

**If using Podman (lab machines):**

```bash
podman run -d --name scc200-db \
  -e POSTGRES_USER=transport \
  -e POSTGRES_PASSWORD=transport_dev \
  -e POSTGRES_DB=transport_db \
  -p 5432:5432 \
  docker.io/library/postgres:16
```

**If using Docker (personal machines):**

```bash
docker run -d --name scc200-db \
  -e POSTGRES_USER=transport \
  -e POSTGRES_PASSWORD=transport_dev \
  -e POSTGRES_DB=transport_db \
  -p 5432:5432 \
  postgres:16
```

### 3. Open the project in the devcontainer

1. Open the project folder in VS Code
2. Press `Ctrl+Shift+P` (or `Cmd+Shift+P` on Mac)
3. Type `Dev Containers: Reopen in Container` and select it
4. Wait for the container to build (first time takes a few minutes)

All Python dependencies are installed automatically.

### 4. Initialise the database schema

Inside the devcontainer terminal:

```bash
PGPASSWORD=transport_dev psql -h localhost -U transport -d transport_db -f scripts/init_db.sql
```

**Important:** This must be run before any ingest scripts. The schema is managed centrally in `init_db.sql` — ingest scripts do not create their own tables.

### 5. Load the stop data

```bash
python -u scripts/ingest_stops.py
```

This downloads NaPTAN data and imports ~8,500 bus stops. Takes about 30 seconds.

### 6. Start the API server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

API docs available at: http://localhost:8080/docs

### 7. Verify everything works

With the server running, open a second terminal and test:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/api/v1/stops/search?q=lancaster
```

## API Endpoints

### Core

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Health check |
| GET | `/api/v1/weather?lat=54.05&lon=-2.80` | Weather proxy (CORS workaround) |

### Stops

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/stops/` | List stops (filter by locality, stop_type) |
| GET | `/api/v1/stops/search?q=lancaster` | Full-text search for stops |
| GET | `/api/v1/stops/nearby?lat=54.05&lon=-2.80` | Find stops within radius |
| GET | `/api/v1/stops/{stop_id}` | Get single stop by ATCOCode |
| GET | `/api/v1/stops/{stop_id}/routes` | Routes serving a stop |

### Disruptions

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/disruptions/` | List disruptions (filter by route, severity, type) |
| GET | `/api/v1/disruptions/active` | Active disruptions sorted by severity |
| GET | `/api/v1/disruptions/{id}` | Single disruption |
| GET | `/api/v1/disruptions/live/buses` | Proxy live SIRI bus feed |
| GET | `/api/v1/disruptions/live/positions` | Latest positions from DB |

## Ingestion Scripts

Run these inside the devcontainer **after** running `init_db.sql`:

| Script | Description |
|--------|-------------|
| `python -u scripts/ingest_stops.py` | Import NaPTAN bus stops (~8,500 stops) |
| `python -u scripts/ingest_live_all.py` | Poll live bus positions (runs continuously, Ctrl+C to stop) |
| `python -u scripts/ingest_timetables.py` | Import timetable data |

## Daily Workflow

### Starting work

1. Start the database (if not already running):
   - Podman: `podman start scc200-db`
   - Docker: `docker start scc200-db`
2. Open the project folder in VS Code
3. Reopen in Container (VS Code should prompt automatically)
4. Start the API server: `uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload`

### Stopping work

1. Close VS Code (the devcontainer stops automatically)
2. Optionally stop the database:
   - Podman: `podman stop scc200-db`
   - Docker: `docker stop scc200-db`

### Resetting the database

If you need a fresh database:

```bash
# On the host machine
podman stop scc200-db && podman rm scc200-db
# Re-run the podman run command from Step 2
# Then inside the devcontainer, re-run Steps 4 and 5
```

## Database Connection Details

| Setting  | Value     |
|----------|-----------|
| Host     | localhost |
| Port     | 5432      |
| User     | transport |
| Password | transport_dev |
| Database | transport_db |
