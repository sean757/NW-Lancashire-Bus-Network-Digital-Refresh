"""
Stops router — endpoints for querying bus stops.
"""

import re
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from app.database import get_db
from app.config import settings

router = APIRouter()


def _service_bounds_params() -> dict:
    return {
        "svc_min_lat": settings.service_min_lat,
        "svc_max_lat": settings.service_max_lat,
        "svc_min_lon": settings.service_min_lon,
        "svc_max_lon": settings.service_max_lon,
    }


@router.get("/")
async def list_stops(
    locality: str | None = None,
    stop_type: str | None = None,
    limit: int = Query(default=50, le=500),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """List stops with optional filtering by locality and type."""
    query = """
        SELECT stop_id, stop_name, locality, bearing, latitude, longitude, stop_type
        FROM stops
        WHERE active = TRUE
          AND latitude  BETWEEN :svc_min_lat AND :svc_max_lat
          AND longitude BETWEEN :svc_min_lon AND :svc_max_lon
    """
    params = _service_bounds_params()

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
                    AND latitude  BETWEEN :svc_min_lat AND :svc_max_lat
                    AND longitude BETWEEN :svc_min_lon AND :svc_max_lon
          AND to_tsvector('english', stop_name) @@ plainto_tsquery('english', :q)
        ORDER BY ts_rank(to_tsvector('english', stop_name), plainto_tsquery('english', :q)) DESC
        LIMIT :limit
    """
    params = {"q": q, "limit": limit, **_service_bounds_params()}
    result = await db.execute(text(query), params)
    rows = result.mappings().all()

    # Fallback 1: token OR-match (so e.g. "Uni Underpass" can match "Underpass")
    if not rows:
        tokens = [t for t in re.split(r"\s+", (q or "").strip()) if t]
        if len(tokens) > 1:
            or_clauses = " OR ".join(
                [f"stop_name ILIKE :tok{i}" for i in range(len(tokens))])
            token_query = f"""
                SELECT stop_id, stop_name, locality, latitude, longitude, stop_type
                FROM stops
                WHERE active = TRUE
                  AND latitude  BETWEEN :svc_min_lat AND :svc_max_lat
                  AND longitude BETWEEN :svc_min_lon AND :svc_max_lon
                  AND ({or_clauses})
                ORDER BY stop_name
                LIMIT :limit
            """
            token_params = {
                **_service_bounds_params(),
                "limit": limit,
                **{f"tok{i}": f"%{tokens[i]}%" for i in range(len(tokens))},
            }
            result = await db.execute(text(token_query), token_params)
            rows = result.mappings().all()

    # Fallback 2: plain substring match
    if not rows:
        fallback_query = """
            SELECT stop_id, stop_name, locality, latitude, longitude, stop_type
            FROM stops
            WHERE active = TRUE
              AND latitude  BETWEEN :svc_min_lat AND :svc_max_lat
              AND longitude BETWEEN :svc_min_lon AND :svc_max_lon
              AND stop_name ILIKE :pattern
            ORDER BY stop_name
            LIMIT :limit
        """
        result = await db.execute(
            text(fallback_query),
            {"pattern": f"%{q}%", "limit": limit, **_service_bounds_params()},
        )
        rows = result.mappings().all()

    return [dict(row) for row in rows]


@router.get("/nearby")
async def nearby_stops(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
    radius_km: float = Query(default=1.0, le=10.0,
                             description="Search radius in km"),
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
              AND latitude  BETWEEN :svc_min_lat AND :svc_max_lat
              AND longitude BETWEEN :svc_min_lon AND :svc_max_lon
        ) AS nearby
        WHERE distance_km <= :radius_km
        ORDER BY distance_km
        LIMIT :limit
    """
    result = await db.execute(
        text(query),
        {
            "lat": lat,
            "lon": lon,
            "radius_km": radius_km,
            "limit": limit,
            **_service_bounds_params(),
        },
    )
    rows = result.mappings().all()
    return [dict(row) for row in rows]


@router.get("/bounds")
async def stops_in_bounds(
    min_lat: float = Query(..., description="Minimum latitude"),
    max_lat: float = Query(..., description="Maximum latitude"),
    min_lon: float = Query(..., description="Minimum longitude"),
    max_lon: float = Query(..., description="Maximum longitude"),
    limit: int = Query(default=200, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Find all active stops within a geographic bounding box."""
    # Clamp to service area bounds so users can't request stops outside coverage.
    clamped_min_lat = max(min_lat, settings.service_min_lat)
    clamped_max_lat = min(max_lat, settings.service_max_lat)
    clamped_min_lon = max(min_lon, settings.service_min_lon)
    clamped_max_lon = min(max_lon, settings.service_max_lon)

    # If the requested bounds don't intersect the service area, return no results.
    if clamped_min_lat > clamped_max_lat or clamped_min_lon > clamped_max_lon:
        return []

    query = """
        SELECT stop_id, stop_name, locality, latitude, longitude, stop_type
        FROM stops
        WHERE active = TRUE
          AND latitude  BETWEEN :min_lat AND :max_lat
          AND longitude BETWEEN :min_lon AND :max_lon
        ORDER BY stop_name
        LIMIT :limit
    """
    result = await db.execute(text(query), {
        "min_lat": clamped_min_lat, "max_lat": clamped_max_lat,
        "min_lon": clamped_min_lon, "max_lon": clamped_max_lon,
        "limit": limit,
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
          AND latitude  BETWEEN :svc_min_lat AND :svc_max_lat
          AND longitude BETWEEN :svc_min_lon AND :svc_max_lon
    """
    result = await db.execute(text(query), {"stop_id": stop_id, **_service_bounds_params()})
    row = result.mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Stop not found")

    return dict(row)


@router.get("/{stop_id}/departures")
async def get_stop_departures_next_24h(stop_id: str, db: AsyncSession = Depends(get_db)):
    """Get scheduled departures for a stop in the next 24 hours."""
    stop_result = await db.execute(
        text(
            """
            SELECT stop_id, stop_name, locality
            FROM stops
            WHERE stop_id = :stop_id
              AND active = TRUE
              AND latitude  BETWEEN :svc_min_lat AND :svc_max_lat
              AND longitude BETWEEN :svc_min_lon AND :svc_max_lon
            """
        ),
        {"stop_id": stop_id, **_service_bounds_params()},
    )
    stop_row = stop_result.mappings().first()
    if not stop_row:
        raise HTTPException(status_code=404, detail="Stop not found")

    now_dt = datetime.now()
    horizon_dt = now_dt + timedelta(hours=24)

    query = text(
        """
        WITH service_days AS (
            SELECT CAST(:today AS DATE) AS service_date
            UNION ALL
            SELECT CAST(:tomorrow AS DATE) AS service_date
        ),
        timetable_with_ts AS (
            SELECT
                t.route_id,
                r.route_name,
                r.operator,
                t.trip_id,
                t.direction,
                last_stop.final_stop_name,
                t.stop_sequence,
                t.departure_time,
                sd.service_date,
                (sd.service_date + t.departure_time) AS departure_datetime
            FROM timetables t
            JOIN routes r
              ON r.route_id = t.route_id
             AND r.active = TRUE
            JOIN service_days sd
              ON 1 = 1
                        LEFT JOIN LATERAL (
                                SELECT s2.stop_name AS final_stop_name
                                FROM timetables t2
                                JOIN stops s2
                                    ON s2.stop_id = t2.stop_id
                                 AND s2.active = TRUE
                                WHERE t2.trip_id = t.trip_id
                                    AND t2.route_id = t.route_id
                                    AND COALESCE(t2.direction, 'outbound') = COALESCE(t.direction, 'outbound')
                                ORDER BY t2.stop_sequence DESC
                                LIMIT 1
                        ) AS last_stop
                            ON TRUE
            WHERE t.stop_id = :stop_id
                            AND COALESCE(t.pickup_allowed, TRUE) = TRUE
              AND (
                    t.valid_from IS NULL
                    OR sd.service_date >= t.valid_from
                  )
              AND (
                    t.valid_until IS NULL
                    OR sd.service_date <= t.valid_until
                  )
              AND (
                    t.days_of_week
                    & CASE EXTRACT(DOW FROM sd.service_date)::int
                        WHEN 1 THEN 1
                        WHEN 2 THEN 2
                        WHEN 3 THEN 4
                        WHEN 4 THEN 8
                        WHEN 5 THEN 16
                        WHEN 6 THEN 32
                        ELSE 64
                      END
                  ) <> 0
        )
        , ranked_calls AS (
            SELECT
                route_id,
                route_name,
                operator,
                trip_id,
                direction,
                final_stop_name,
                stop_sequence,
                departure_time,
                service_date,
                departure_datetime,
                ROW_NUMBER() OVER (
                    PARTITION BY route_id, trip_id, service_date
                    ORDER BY departure_datetime DESC, stop_sequence DESC
                ) AS call_rank
            FROM timetable_with_ts
        )
        SELECT
            route_id,
            route_name,
            operator,
            trip_id,
            direction,
            final_stop_name,
            stop_sequence,
            departure_time,
            service_date,
            departure_datetime
        FROM ranked_calls
        WHERE call_rank = 1
          AND departure_datetime >= :now_dt
          AND departure_datetime < :horizon_dt
        ORDER BY departure_datetime ASC, route_name ASC
        """
    )

    result = await db.execute(
        query,
        {
            "stop_id": stop_id,
            "today": now_dt.date(),
            "tomorrow": (now_dt.date() + timedelta(days=1)),
            "now_dt": now_dt,
            "horizon_dt": horizon_dt,
        },
    )
    rows = result.mappings().all()

    departures = []
    for row in rows:
        departures.append(
            {
                "route_id": row["route_id"],
                "route_name": row["route_name"],
                "operator": row["operator"],
                "trip_id": row["trip_id"],
                "direction": row["direction"],
                "final_destination_name": row["final_stop_name"],
                "stop_sequence": row["stop_sequence"],
                "service_date": row["service_date"].isoformat() if row["service_date"] else None,
                "departure_time": row["departure_time"].strftime("%H:%M") if row["departure_time"] else None,
                "departure_datetime": row["departure_datetime"].isoformat() if row["departure_datetime"] else None,
            }
        )

    return {
        "stop": {
            "stop_id": stop_row["stop_id"],
            "stop_name": stop_row["stop_name"],
            "locality": stop_row["locality"],
        },
        "window_hours": 24,
        "generated_at": now_dt.isoformat(),
        "departures_count": len(departures),
        "departures": departures,
    }


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
