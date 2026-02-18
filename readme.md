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

Run this on the **host machine** outside of the devcontainer:

**If using Podman (lab machines):**

```bash
podman run -d --name scc200-db \
  -e POSTGRES_USER=transport \
  -e POSTGRES_PASSWORD=transport_dev \
  -e POSTGRES_DB=transport_db \
  -p 5432:5432 \
  docker.io/library/postgres:16
```

**If using Docker:**

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

Once inside the container, you'll have Python 3.12 and all project dependencies installed automatically.

### 4. Verify everything works

Open a terminal inside the devcontainer and run:

```bash
python -c 'import psycopg2; conn = psycopg2.connect(host="host.containers.internal", port=5432, user="transport", password="transport_dev", dbname="transport_db"); print("Connected"); conn.close()'
```

If you see `Connected`, it's all set up

> **Troubleshooting:** If `host.containers.internal` doesn't work, try `172.17.0.1` or `host.docker.internal` instead.

## Daily Workflow

### Starting work

1. Start the database (if not already running):
   - Podman: `podman start scc200-db`
   - Docker: `docker start scc200-db`
2. Open the project folder in VS Code
3. Reopen in Container (VS Code should prompt automatically)

### Stopping work

1. Close VS Code (the devcontainer stops automatically)
2. Optionally stop the database:
   - Podman: `podman stop scc200-db`
   - Docker: `docker stop scc200-db`

### Resetting the database

If you need a fresh database:

```bash
# Podman
podman stop scc200-db && podman rm scc200-db

# Then re-run the podman run command
```

## Running the API server

Inside the devcontainer:

```bash
cd /workspace
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

API docs available at: http://localhost:8000/docs


### Database Connection Details

| Setting  | Value                      |
|----------|----------------------------|
| Host     | host.containers.internal   |
| Port     | 5432                       |
| User     | transport                  |
| Password | transport_dev              |
| Database | transport_db               |