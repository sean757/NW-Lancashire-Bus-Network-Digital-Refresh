"""
SCC200 Transport API — Main application entry point.
"""

import asyncio
import logging
import os
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.config import settings
from app.database import init_db, close_db
from app.routers import stops, disruptions, journey, routes, rail
from app.services.route_cache import route_cache
from app.services.vehicle_cache import vehicle_cache
from app.services.trust_listener import trust_listener
import httpx

log = logging.getLogger(__name__)

# Directory containing the ingestion scripts (../../scripts relative to this file)
_SCRIPTS_DIR = os.path.realpath(os.path.join(
    os.path.dirname(__file__), "..", "scripts"))

# Path to the front-end directory (app/ sits one level below the project root)
FRONTEND_DIR = os.path.realpath(os.path.join(
    os.path.dirname(__file__), "..", "front-end"))
_FRONTEND_INDEX = os.path.join(FRONTEND_DIR, "index.html")


app = FastAPI(
    title=settings.app_title,
    version="0.1.0",
    docs_url="/docs",
)

# CORS — allow the frontend to make requests to this backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten this to your frontend URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    await init_db()
    # Load route cache in the background so the server can start accepting
    # requests immediately.  The /health endpoint exposes cache_loaded status.
    asyncio.create_task(_load_cache_then_start())
    # Start the vehicle cache independently so a slow/stuck DB query in the
    # route cache doesn't block live bus position polling.
    asyncio.create_task(_start_vehicle_cache())


async def _start_vehicle_cache():
    """Perform an initial vehicle cache fetch then start background refresh."""
    try:
        await vehicle_cache.refresh()
    except Exception as exc:
        log.error("Initial vehicle cache refresh failed: %s", exc)
    asyncio.create_task(vehicle_cache.refresh_loop())


async def _load_cache_then_start():
    """Load route cache, then start remaining background loops."""
    await route_cache.load()
    asyncio.create_task(route_cache.refresh_loop())
    # Start the TRUST STOMP listener for live rail movement data.
    trust_listener.start()
    # Start the 24-hour data ingestion loop.
    asyncio.create_task(data_refresh_loop())


@app.on_event("shutdown")
async def shutdown():
    trust_listener.stop()
    await close_db()


# ==========================================
# 24-hour data refresh
# ==========================================

# Ingestion scripts to run, in order.  Each entry is a human-readable label
# paired with the script filename inside _SCRIPTS_DIR.
_INGEST_SCRIPTS: list[tuple[str, str]] = [
    ("stops", "ingest_stops.py"),
    ("bus timetables", "ingest_timetables.py"),
    ("rail timetables", "ingest_rail.py"),
]

_REFRESH_INTERVAL = 24 * 3600     # 24 hours
_RETRY_INTERVAL = 30 * 60         # 30 minutes


async def _run_ingest_script(label: str, script: str) -> bool:
    """Run a single ingestion script as a subprocess.

    Returns True on success, False on failure.  Errors are logged but
    never propagated — the previous data remains intact in the database.
    """
    script_path = os.path.join(_SCRIPTS_DIR, script)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            log.info("Data refresh [%s] succeeded", label)
            return True
        else:
            log.warning(
                "Data refresh [%s] failed (exit %s):\n%s",
                label, proc.returncode,
                stdout.decode(errors="replace")[-2000],
            )
    except Exception:
        log.exception("Data refresh [%s] raised an exception", label)
    return False


async def data_refresh_loop():
    """Periodically re-ingest stop and timetable data.

    On each 24-hour cycle every ingestion script is attempted.  Scripts
    that fail are retried every 30 minutes until they succeed (or the
    next 24-hour cycle begins, whichever comes first).  Because the
    ingestion scripts use INSERT … ON CONFLICT, the old data is preserved
    on failure.
    """
    while True:
        await asyncio.sleep(_REFRESH_INTERVAL)

        pending = list(_INGEST_SCRIPTS)  # scripts still to succeed
        while pending:
            still_failing: list[tuple[str, str]] = []
            for label, script in pending:
                ok = await _run_ingest_script(label, script)
                if not ok:
                    still_failing.append((label, script))
            pending = still_failing

            if pending:
                labels = ", ".join(l for l, _ in pending)
                log.warning(
                    "Data refresh: %d script(s) still failing (%s) — "
                    "retrying in 30 min",
                    len(pending), labels,
                )
                await asyncio.sleep(_RETRY_INTERVAL)

        # After successful ingestion, reload the in-memory route cache
        # so the API reflects the new data.
        log.info("All ingest scripts succeeded — reloading route cache")
        await route_cache.load()


# ==========================================
# Health Check
# ==========================================

@app.get("/health")
async def health_check():
    return {
        "status": "ok",
        "cache_loaded": route_cache._loaded,
        "routes_cached": len(route_cache.routes),
        "stops_with_routes": len(route_cache.stop_routes),
    }


# ==========================================
# Weather Proxy
# ==========================================
# The university transport API has CORS disabled,
# so the frontend can't call it directly from the browser.
# This endpoint proxies the request through our backend.

@app.get("/api/v1/weather")
async def get_weather(lat: float = 54.05, lon: float = -2.80):
    """Proxy weather data from the university transport API."""
    url = f"https://transport.scc.lancs.ac.uk/weather?lat={lat}&lon={lon}"
    async with httpx.AsyncClient(verify=False) as client:
        response = await client.get(url, timeout=10)
        return response.json()


# ==========================================
# Routers
# ==========================================

app.include_router(stops.router, prefix="/api/v1/stops", tags=["Stops"])
app.include_router(disruptions.router,
                   prefix="/api/v1/disruptions", tags=["Disruptions"])
app.include_router(journey.router, prefix="/api/v1/journey", tags=["Journey"])
app.include_router(routes.router, prefix="/api/v1/routes", tags=["Routes"])
app.include_router(rail.router, prefix="/api/v1/rail", tags=["Rail"])


# ==========================================
# Frontend
# ==========================================
# Serve index.html at the root URL.  The explicit route is registered before
# the StaticFiles mount so that GET / always returns the HTML page.

@app.get("/", include_in_schema=False)
async def serve_index():
    """Serve the frontend application."""
    resolved = os.path.realpath(_FRONTEND_INDEX)
    if not resolved.startswith(FRONTEND_DIR):
        from fastapi import HTTPException
        raise HTTPException(status_code=404)
    return FileResponse(resolved)


# Mount the entire front-end directory so the browser can load the CSS, JS,
# images, and Leaflet library referenced by index.html.  This must come AFTER
# all API route registrations so it does not shadow any API endpoints.
app.mount("/", StaticFiles(directory=FRONTEND_DIR), name="frontend")
