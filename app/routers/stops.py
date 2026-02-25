"""
Stops router — endpoints for querying bus stops.
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from app.database import get_db

router = APIRouter()


@router.get("/")
async def list_stops(
    locality: str | None = None,
    stop_type: str | None = None,
    limit: int = Query(default=50, le=500),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """List stops with optional filtering by locality and type."""
    query = "SELECT stop_id, stop_name, locality, bearing, latitude, longitude, stop_type FROM stops WHERE active = TRUE"
    params = {}

    if locality:
        query += " AND locality ILIKE :locality"
        params["locality"] = f"%{locality}%"

    if stop_type:
        query += " AND stop_type = :stop_type"
        params["stop_type"] = stop_type

    query += " ORDER BY stop_name LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset

    result = await db.execute(text(query), params)
    rows = result.mappings().all()
    return [dict(row) for row in rows]


@router.get("/search")
async def search_stops(
    q: str = Query(..., min_length=2, description="Search term for stop name"),
    limit: int = Query(default=20, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Full-text search for stops by name."""
    query = """
        SELECT stop_id, stop_name, locality, latitude, longitude, stop_type
        FROM stops
        WHERE active = TRUE
          AND to_tsvector('english', stop_name) @@ plainto_tsquery('english', :q)
        ORDER BY ts_rank(to_tsvector('english', stop_name), plainto_tsquery('english', :q)) DESC
        LIMIT :limit
    """
    result = await db.execute(text(query), {"q": q, "limit": limit})
    rows = result.mappings().all()

    # Fallback to ILIKE if full-text search returns nothing
    if not rows:
        fallback_query = """
            SELECT stop_id, stop_name, locality, latitude, longitude, stop_type
            FROM stops
            WHERE active = TRUE AND stop_name ILIKE :pattern
            ORDER BY stop_name
            LIMIT :limit
        """
        result = await db.execute(text(fallback_query), {"pattern": f"%{q}%", "limit": limit})
        rows = result.mappings().all()

    return [dict(row) for row in rows]


@router.get("/nearby")
async def nearby_stops(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
    radius_km: float = Query(default=1.0, le=10.0, description="Search radius in km"),
    limit: int = Query(default=20, le=100),
    db: AsyncSession = Depends(get_db),
):
    """Find stops within a radius of a given coordinate using Haversine formula."""
    query = """
        SELECT stop_id, stop_name, locality, latitude, longitude, stop_type,
            (6371 * acos(
                cos(radians(:lat)) * cos(radians(latitude)) *
                cos(radians(longitude) - radians(:lon)) +
                sin(radians(:lat)) * sin(radians(latitude))
            )) AS distance_km
        FROM stops
        WHERE active = TRUE
        HAVING distance_km <= :radius_km
        ORDER BY distance_km
        LIMIT :limit
    """
    # PostgreSQL doesn't support HAVING without GROUP BY on non-aggregate,
    # so we use a subquery instead
    query = """
        SELECT * FROM (
            SELECT stop_id, stop_name, locality, latitude, longitude, stop_type,
                (6371 * acos(
                    cos(radians(:lat)) * cos(radians(latitude)) *
                    cos(radians(longitude) - radians(:lon)) +
                    sin(radians(:lat)) * sin(radians(latitude))
                )) AS distance_km
            FROM stops
            WHERE active = TRUE
        ) AS nearby
        WHERE distance_km <= :radius_km
        ORDER BY distance_km
        LIMIT :limit
    """
    result = await db.execute(text(query), {
        "lat": lat, "lon": lon, "radius_km": radius_km, "limit": limit
    })
    rows = result.mappings().all()
    return [dict(row) for row in rows]


@router.get("/{stop_id}")
async def get_stop(stop_id: str, db: AsyncSession = Depends(get_db)):
    """Get a single stop by its ID (ATCOCode)."""
    query = """
        SELECT stop_id, stop_name, locality, bearing, latitude, longitude, stop_type, created_at
        FROM stops
        WHERE stop_id = :stop_id AND active = TRUE
    """
    result = await db.execute(text(query), {"stop_id": stop_id})
    row = result.mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Stop not found")

    return dict(row)


@router.get("/{stop_id}/routes")
async def get_stop_routes(stop_id: str, db: AsyncSession = Depends(get_db)):
    """Get all routes that serve a given stop."""
    query = """
        SELECT r.route_id, r.route_name, r.operator, r.route_type, rs.direction, rs.stop_sequence
        FROM route_stops rs
        JOIN routes r ON r.route_id = rs.route_id
        WHERE rs.stop_id = :stop_id AND r.active = TRUE
        ORDER BY r.route_name
    """
    result = await db.execute(text(query), {"stop_id": stop_id})
    rows = result.mappings().all()
    return [dict(row) for row in rows]