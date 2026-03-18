"""
SCC200 Transport API — Main application entry point.
"""

import asyncio
import os
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
    # Load route cache and start background refresh (every hour)
    await route_cache.load()
    asyncio.create_task(route_cache.refresh_loop())
    # Perform an initial vehicle cache fetch then start background refresh (every 30 s).
    # The initial fetch ensures data is available immediately on first user request.
    await vehicle_cache.refresh()
    asyncio.create_task(vehicle_cache.refresh_loop())
    # Start the TRUST STOMP listener for live rail movement data.
    trust_listener.start()


@app.on_event("shutdown")
async def shutdown():
    trust_listener.stop()
    await close_db()


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
