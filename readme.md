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

### 6. Load timetable data

```bash
python -u scripts/ingest_timetables.py
```

Downloads TransXChange timetable ZIPs for all configured operators and inserts
routes, stop sequences, and trip times into the database.  Data is sanitised
during ingestion (invalid coordinates and malformed times are skipped with
warnings).

### 7. Export GTFS data for OpenTripPlanner

The journey planner uses [OpenTripPlanner (OTP)](https://docs.opentripplanner.org/)
as its routing engine.  OTP requires data in
[GTFS format](https://gtfs.org/documentation/schedule/reference/).

Generate the GTFS export from the ingested database:

```bash
python -u scripts/export_gtfs.py gtfs_export.zip
```

This exports a sanitised GTFS ZIP containing `agency.txt`, `stops.txt`,
`routes.txt`, `trips.txt`, `stop_times.txt`, and `calendar.txt`.

### 8. Start OpenTripPlanner

OTP is a Java application.  Download the latest OTP 2.x JAR from the
[OTP releases page](https://github.com/opentripplanner/OpenTripPlanner/releases)
and place the GTFS ZIP and an OpenStreetMap extract in the same directory.

```bash
# Download OTP (replace X.Y.Z with the latest version)
wget https://github.com/opentripplanner/OpenTripPlanner/releases/download/v2.8.1/otp-shaded-2.8.1.jar

# Download an OSM extract for NW Lancashire/Lancashire
# (e.g. from https://download.geofabrik.de/europe/great-britain/england/lancashire.html)
wget https://download.geofabrik.de/europe/united-kingdom/england/lancashire-latest.osm.pbf

# Create OTP data directory
mkdir -p otp-data
cp gtfs_export.zip otp-data/
cp lancashire-latest.osm.pbf otp-data/

# Build the OTP graph (takes a few minutes)
java -Xmx4G -jar otp-shaded-2.8.1.jar --build --save otp-data

# Start OTP server (default port 8080)
# IMPORTANT: OTP uses port 8080 by default — the API backend uses the same
# port in development.  Run OTP on a different port (e.g. 9090) and update
# the OTP_URL environment variable accordingly.
java -Xmx4G -jar otp-shaded-2.8.1.jar --load otp-data --port 9090
```

Set the OTP URL before starting the API server:

```bash
export OTP_URL=http://localhost:9090
```

Or add it to a `.env` file in the project root:

```ini
OTP_URL=http://localhost:9090
```

### 9. Start the API server

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

API docs available at: http://localhost:8080/docs

### 10. Verify front-end works

Open `http://localhost:3000` in your browser to view the front-end webpage

### 11. Verify everything works

With both OTP and the API server running, open a second terminal and test:

```bash
curl http://localhost:8080/health
curl http://localhost:8080/api/v1/stops/search?q=lancaster
curl -X POST http://localhost:8080/api/v1/journey/plan \
  -H 'Content-Type: application/json' \
  -d '{"origin_stop_id":"2500LAA11791","destination_stop_id":"2500LAA11792"}'
```

## Environment Variables

| Variable     | Default               | Description                              |
|--------------|-----------------------|------------------------------------------|
| `OTP_URL`    | `http://localhost:8080` | Base URL of the OTP server             |
| `OTP_ROUTER` | `default`             | OTP router name (v1 REST fallback only)  |
| `DB_HOST`    | `localhost`           | PostgreSQL host                          |
| `DB_PORT`    | `5432`                | PostgreSQL port                          |
| `DB_USER`    | `transport`           | PostgreSQL user                          |
| `DB_PASSWORD`| `transport_dev`       | PostgreSQL password                      |
| `DB_NAME`    | `transport_db`        | PostgreSQL database name                 |

## Architecture Overview

```
Browser  ──── fetch ────►  FastAPI backend (port 8080)
                               │
                        POST /api/v1/journey/plan
                               │
                        app/services/otp_client.py
                               │
                    OTP v2 GraphQL (POST /otp/gtfs/v1)
                    ── fallback ──►  OTP v1 REST
                               │
                    OpenTripPlanner (port 9090)
                        loaded with gtfs_export.zip
```

The backend resolves stop IDs to coordinates (via an in-memory cache or the
database), delegates route planning to OTP, and translates the OTP itinerary
format back into the existing JSON response shape used by the front end.

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

### Journey Planning

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/api/v1/journey/plan` | Plan a journey via OpenTripPlanner |

**Request body:**
```json
{
  "origin_stop_id": "2500LAA11791",
  "destination_stop_id": "2500LAA11792",
  "departure_time": "08:30",
  "departure_date": "2025-03-10",
  "preference": "fastest",
  "walking_speed": "medium"
}
```

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
| `python -u scripts/ingest_timetables.py` | Import timetable data (with data sanitisation) |
| `python -u scripts/ingest_live_all.py` | Poll live bus positions (runs continuously, Ctrl+C to stop) |
| `python -u scripts/export_gtfs.py [output.zip]` | Export sanitised GTFS ZIP for OpenTripPlanner |

## Daily Workflow

### Starting work

1. Start the database (if not already running):
   - Podman: `podman start scc200-db`
   - Docker: `docker start scc200-db`
2. Start OTP: `java -Xmx4G -jar otp-X.Y.Z-shaded.jar --load otp-data --port 9090`
3. Open the project folder in VS Code
4. Reopen in Container (VS Code should prompt automatically)
5. Start the API server: `OTP_URL=http://localhost:9090 uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload`

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
# Then inside the devcontainer, re-run Steps 4–7
```

## Database Connection Details

| Setting  | Value     |
|----------|-----------|
| Host     | localhost |
| Port     | 5432      |
| User     | transport |
| Password | transport_dev |
| Database | transport_db |
