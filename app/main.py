"""
SCC200 Transport API — Main application entry point.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.config import settings
from app.database import init_db, close_db
import httpx


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


@app.on_event("shutdown")
async def shutdown():
    await close_db()


# ==========================================
# Health Check
# ==========================================

@app.get("/health")
async def health_check():
    return {"status": "ok"}


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
# Routers (uncomment as they're built)
# ==========================================

# from app.routers import stops, journey, disruptions
# app.include_router(stops.router, prefix="/api/v1/stops", tags=["Stops"])
# app.include_router(journey.router, prefix="/api/v1/journey", tags=["Journey"])
# app.include_router(disruptions.router, prefix="/api/v1/disruptions", tags=["Disruptions"])