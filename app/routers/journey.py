"""
Journey planner router — plan journeys between two stops using OpenTripPlanner (OTP).

OTP must be running and loaded with GTFS data exported by scripts/export_gtfs.py.
Configure the OTP URL with the OTP_URL environment variable (default: http://localhost:8080).
"""

import logging
from datetime import date, datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services import otp_client
from app.services.route_cache import route_cache

router = APIRouter()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------

class JourneyRequest(BaseModel):
    origin_stop_id: Optional[str] = None
    origin_lat: Optional[float] = None
    origin_lon: Optional[float] = None
    destination_stop_id: Optional[str] = None
    destination_lat: Optional[float] = None
    destination_lon: Optional[float] = None
    departure_time: Optional[str] = None   # HH:MM or HH:MM:SS
    # YYYY-MM-DD, DD/MM/YYYY, or DD-MM-YYYY
    departure_date: Optional[str] = None
    preference: Optional[str] = "fastest"  # "fastest" | "least-changes"
    walking_speed: Optional[str] = "medium"  # "slow" | "medium" | "fast"
    # False = depart after, True = arrive before
    arrive_by: Optional[bool] = False


# ---------------------------------------------------------------------------
# Location resolution helpers
# ---------------------------------------------------------------------------

async def _stop_info_from_db(stop_id: str, db: AsyncSession):
    """Return (lat, lon, name) for a stop_id from the database, or None."""
    result = await db.execute(
        text("SELECT stop_name, latitude, longitude FROM stops WHERE stop_id = :sid AND active = TRUE"),
        {"sid": stop_id},
    )
    row = result.mappings().first()
    return (row["latitude"], row["longitude"], row["stop_name"]) if row else None


async def _find_nearest_stop(lat: float, lon: float, db: AsyncSession) -> Optional[str]:
    """Return the stop_id of the nearest active stop to (lat, lon)."""
    query = """
        SELECT stop_id
        FROM (
            SELECT stop_id,
                (6371 * acos(
                    cos(radians(:lat)) * cos(radians(latitude)) *
                    cos(radians(longitude) - radians(:lon)) +
                    sin(radians(:lat)) * sin(radians(latitude))
                )) AS distance_km
            FROM stops
            WHERE active = TRUE
        ) AS nearby
        ORDER BY distance_km
        LIMIT 1
    """
    result = await db.execute(text(query), {"lat": lat, "lon": lon})
    row = result.mappings().first()
    return row["stop_id"] if row else None


async def _resolve_location(
    stop_id: Optional[str],
    lat: Optional[float],
    lon: Optional[float],
    db: AsyncSession,
):
    """
    Resolve a location to (lat, lon, stop_id, name).
    stop_id takes priority over lat/lon.
    Returns (lat, lon, stop_id_or_None, name) — or (None, None, None, "")
    if the location cannot be resolved.
    """
    if stop_id:
        # Try in-memory cache first (avoids a DB round-trip)
        coords = route_cache.stop_coords.get(stop_id)
        name = route_cache.stop_names.get(stop_id, "")
        if coords:
            return coords[0], coords[1], stop_id, name or stop_id

        # Fall back to database
        info = await _stop_info_from_db(stop_id, db)
        if info:
            return info[0], info[1], stop_id, info[2]

    if lat is not None and lon is not None:
        nearest = await _find_nearest_stop(lat, lon, db)
        name = route_cache.stop_names.get(nearest, "") if nearest else ""
        return lat, lon, nearest, name

    return None, None, None, ""


async def _fetch_leg_waypoints(
    db: AsyncSession,
    route_id: str,
    direction: str,
    from_stop: Optional[str],
    to_stop: Optional[str],
):
    """Return ordered [{lat, lon}, ...] waypoints for a bus leg.

    Uses stored route_waypoints geometry and slices by from/to stop when both
    stop IDs are present.

    For loop/variant routes where stop IDs may appear multiple times, this picks
    the nearest forward (from_seq <= to_seq) pair. If no valid bounded slice is
    found, returns an empty list so callers can fall back to OTP geometry.
    """
    if not route_id:
        return []

    direction = direction or "outbound"

    if from_stop and to_stop:
        query = """
                        WITH from_candidates AS (
                                SELECT variant_id, sequence
                                FROM route_waypoints
                                WHERE route_id = :route_id
                                    AND direction = :direction
                                    AND stop_id = :from_stop
                        ),
                        to_candidates AS (
                                SELECT variant_id, sequence
                                FROM route_waypoints
                                WHERE route_id = :route_id
                                    AND direction = :direction
                                    AND stop_id = :to_stop
                        ),
                        best_pair AS (
                SELECT
                                        f.variant_id AS variant_id,
                                        f.sequence AS from_seq,
                                        t.sequence AS to_seq
                                FROM from_candidates f
                                JOIN to_candidates t
                                    ON t.variant_id = f.variant_id
                                 AND t.sequence >= f.sequence
                                ORDER BY (t.sequence - f.sequence), f.sequence
                                LIMIT 1
            )
            SELECT w.latitude, w.longitude
            FROM route_waypoints w
                        JOIN best_pair
                            ON w.variant_id = best_pair.variant_id
            WHERE w.route_id  = :route_id
              AND w.direction = :direction
                            AND w.sequence >= best_pair.from_seq
                            AND w.sequence <= best_pair.to_seq
            ORDER BY w.sequence
        """
        result = await db.execute(
            text(query),
            {
                "route_id": route_id,
                "direction": direction,
                "from_stop": from_stop,
                "to_stop": to_stop,
            },
        )
        rows = result.mappings().all()
        if rows:
            return [{"lat": row["latitude"], "lon": row["longitude"]} for row in rows]
        return []

    fallback_query = """
                WITH best_variant AS (
                        SELECT variant_id
                        FROM route_waypoints
                        WHERE route_id = :route_id
                            AND direction = :direction
                        GROUP BY variant_id
                        ORDER BY COUNT(*) DESC, variant_id
                        LIMIT 1
                )
        SELECT latitude, longitude
                FROM route_waypoints w
                JOIN best_variant b ON b.variant_id = w.variant_id
        WHERE route_id = :route_id
          AND direction = :direction
        ORDER BY sequence
    """
    fallback = await db.execute(
        text(fallback_query),
        {"route_id": route_id, "direction": direction},
    )
    rows = fallback.mappings().all()
    return [{"lat": row["latitude"], "lon": row["longitude"]} for row in rows]


# ---------------------------------------------------------------------------
# Time / date parsing
# ---------------------------------------------------------------------------

def _parse_departure_time_date(req: JourneyRequest):
    """Parse departure time and date from the request, applying sensible defaults."""
    # Time
    if req.departure_time:
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                dep_time = datetime.strptime(req.departure_time, fmt).time()
                break
            except ValueError:
                continue
        else:
            dep_time = datetime.now().time()
    else:
        dep_time = datetime.now().time()

    # Date
    if req.departure_date:
        dep_date = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
            try:
                dep_date = datetime.strptime(req.departure_date, fmt).date()
                break
            except ValueError:
                continue
        if dep_date is None:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Invalid departure_date format. "
                    "Accepted formats: YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY"
                ),
            )
    else:
        dep_date = date.today()

    return dep_time, dep_date


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@router.post("/plan")
async def plan_journey(req: JourneyRequest, db: AsyncSession = Depends(get_db)):
    """
    Plan a journey between two stops using OpenTripPlanner.

    Accepts a stop_id (ATCOCode) or a lat/lon pair for both origin and
    destination.  Returns up to 10 itineraries sorted by arrival time,
    each consisting of bus and walking legs in the same format as before.
    """
    # --- Resolve origin ---
    origin_lat, origin_lon, origin_stop_id, origin_name = await _resolve_location(
        req.origin_stop_id, req.origin_lat, req.origin_lon, db
    )
    if origin_lat is None:
        raise HTTPException(
            status_code=400,
            detail="Origin stop required (provide stop_id or lat/lon coordinates).",
        )

    # --- Resolve destination ---
    dest_lat, dest_lon, dest_stop_id, dest_name = await _resolve_location(
        req.destination_stop_id, req.destination_lat, req.destination_lon, db
    )
    if dest_lat is None:
        raise HTTPException(
            status_code=400,
            detail="Destination stop required (provide stop_id or lat/lon coordinates).",
        )

    # --- Parse departure time / date ---
    dep_time, dep_date = _parse_departure_time_date(req)

    # --- Plan via OTP ---
    try:
        journeys = await otp_client.plan_journey(
            origin_lat=origin_lat,
            origin_lon=origin_lon,
            dest_lat=dest_lat,
            dest_lon=dest_lon,
            dep_time=dep_time,
            dep_date=dep_date,
            num_itineraries=10,
            preference=req.preference or "fastest",
            walking_speed=req.walking_speed or "medium",
            arrive_by=req.arrive_by or False,
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        logger.error("OTP unavailable: %s", exc)
        raise HTTPException(
            status_code=503,
            detail=(
                "Journey planning service (OpenTripPlanner) is currently unavailable. "
                "Please ensure OTP is running and loaded with GTFS data "
                "(see scripts/export_gtfs.py and the README for setup instructions)."
            ),
        )
    except httpx.HTTPStatusError as exc:
        logger.error("OTP HTTP error %s: %s", exc.response.status_code, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Journey planning service returned an error: HTTP {exc.response.status_code}.",
        )

    # Enrich bus legs with DB-backed waypoints so the frontend can draw
    # road-following geometry directly from this endpoint response.
    waypoint_cache = {}
    for journey in journeys:
        for leg in (journey.get("legs") or []):
            if (leg.get("mode") or "").lower() != "bus":
                continue
            route_id = leg.get("route_id") or ""
            direction = leg.get("direction") or "outbound"
            from_stop = leg.get("origin_stop_id")
            to_stop = leg.get("destination_stop_id")
            cache_key = (route_id, direction, from_stop, to_stop)
            if cache_key not in waypoint_cache:
                waypoint_cache[cache_key] = await _fetch_leg_waypoints(
                    db, route_id, direction, from_stop, to_stop
                )
            leg["waypoints"] = waypoint_cache[cache_key]

    return {
        "origin": {"stop_id": origin_stop_id, "name": origin_name},
        "destination": {"stop_id": dest_stop_id, "name": dest_name},
        "departure_date": dep_date.isoformat(),
        "departure_time": dep_time.strftime("%H:%M"),
        "results_count": len(journeys),
        "journeys": journeys,
    }
