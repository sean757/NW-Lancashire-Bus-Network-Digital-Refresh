"""
Routes router — endpoints for querying bus route geometry.
"""

import re
from datetime import time
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from app.database import get_db

router = APIRouter()


def _normalise_time_string(raw: str | None) -> str | None:
    """Accept HH:MM or HH:MM:SS and return HH:MM:SS, else None."""
    if not raw:
        return None
    s = raw.strip()
    if re.fullmatch(r"\d{2}:\d{2}", s):
        return f"{s}:00"
    if re.fullmatch(r"\d{2}:\d{2}:\d{2}", s):
        return s
    return None


@router.get("/{route_id}/waypoints")
async def get_route_waypoints(
    route_id: str,
    direction: str = Query(
        default="outbound", description="Route direction (outbound/inbound)"),
    from_stop: str | None = Query(
        default=None, description="Origin stop ID — only return waypoints from this stop onward"),
    to_stop: str | None = Query(
        default=None, description="Destination stop ID — only return waypoints up to this stop"),
    db: AsyncSession = Depends(get_db),
):
    """
    Return the ordered waypoints (lat/lon) for a route direction.

    When *from_stop* and *to_stop* are provided only the waypoints that lie
    between (and including) those two stops are returned.  This allows the
    front-end to draw the portion of the route actually travelled by a
    specific journey leg rather than the full route.

    Falls back to returning all waypoints when the stop IDs are not found in
    the waypoints table.
    """
    # Verify the route exists
    route_check = await db.execute(
        text("SELECT route_id FROM routes WHERE route_id = :rid AND active = TRUE"),
        {"rid": route_id},
    )
    if not route_check.scalar():
        raise HTTPException(status_code=404, detail="Route not found")

    if from_stop and to_stop:
        # For loop/variant routes, stop IDs can appear multiple times.
        # Choose the nearest forward from->to pair.
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
                        JOIN best_pair b ON b.variant_id = w.variant_id
            WHERE w.route_id  = :route_id
                            AND w.direction = :direction
                            AND w.sequence >= b.from_seq
                            AND w.sequence <= b.to_seq
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
    else:
        query = """
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
                        WHERE w.route_id = :route_id
                            AND w.direction = :direction
                        ORDER BY w.sequence
        """
        result = await db.execute(
            text(query),
            {"route_id": route_id, "direction": direction},
        )

    rows = result.mappings().all()
    return [{"lat": row["latitude"], "lon": row["longitude"]} for row in rows]


@router.get("/{route_id}/stops-between")
async def get_route_stops_between(
    route_id: str,
    from_stop: str = Query(..., description="Origin stop ID for the leg"),
    to_stop: str = Query(..., description="Destination stop ID for the leg"),
    direction: str | None = Query(
        default=None,
        description="Optional direction filter (outbound/inbound)",
    ),
    departure_time: str | None = Query(
        default=None,
        description="Optional leg departure time (HH:MM or HH:MM:SS) used to pick the closest trip",
    ),
    db: AsyncSession = Depends(get_db),
):
    """
    Return ordered stops (with arrival/departure times) between two stops.

    The endpoint finds a suitable trip for the given route where both *from_stop*
    and *to_stop* occur in forward order, then returns all intermediate stop calls
    inclusive of the endpoints. Works for both bus and rail as long as timetable
    rows exist in the shared timetables table.
    """
    # Verify the route exists
    route_check = await db.execute(
        text("SELECT route_id FROM routes WHERE route_id = :rid AND active = TRUE"),
        {"rid": route_id},
    )
    if not route_check.scalar():
        raise HTTPException(status_code=404, detail="Route not found")

    dep_time_norm = _normalise_time_string(departure_time)
    if departure_time and dep_time_norm is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid departure_time format. Use HH:MM or HH:MM:SS.",
        )

    if direction:
        rows_sql = text(
            """
            SELECT
                t.trip_id,
                t.stop_id,
                t.stop_sequence,
                t.arrival_time,
                t.departure_time,
                t.direction,
                s.stop_name
            FROM timetables t
            LEFT JOIN stops s ON s.stop_id = t.stop_id
            WHERE t.route_id = :route_id
              AND t.direction = :direction
            ORDER BY t.trip_id, t.stop_sequence
            """
        )
        all_rows = (
            await db.execute(rows_sql, {"route_id": route_id, "direction": direction})
        ).mappings().all()
    else:
        rows_sql = text(
            """
            SELECT
                t.trip_id,
                t.stop_id,
                t.stop_sequence,
                t.arrival_time,
                t.departure_time,
                t.direction,
                s.stop_name
            FROM timetables t
            LEFT JOIN stops s ON s.stop_id = t.stop_id
            WHERE t.route_id = :route_id
            ORDER BY t.trip_id, t.stop_sequence
            """
        )
        all_rows = (
            await db.execute(rows_sql, {"route_id": route_id})
        ).mappings().all()

    if not all_rows:
        raise HTTPException(
            status_code=404,
            detail="No timetable rows found for this route.",
        )

    def _to_secs(t: time | None) -> int:
        if t is None:
            return 0
        return t.hour * 3600 + t.minute * 60 + t.second

    target_secs: int | None = None
    if dep_time_norm:
        h, m, s = [int(p) for p in dep_time_norm.split(":")]
        target_secs = h * 3600 + m * 60 + s

    # Group rows by trip_id
    by_trip: dict[str, list[dict]] = {}
    for r in all_rows:
        by_trip.setdefault(r["trip_id"], []).append(r)

    chosen_trip_id = None
    chosen_direction = None
    chosen_slice: list[dict] = []
    best_key = None

    for trip_id, trip_rows in by_trip.items():
        from_idx = next(
            (i for i, r in enumerate(trip_rows) if r["stop_id"] == from_stop),
            None,
        )
        if from_idx is None:
            continue

        to_idx = next(
            (
                i
                for i in range(from_idx + 1, len(trip_rows))
                if trip_rows[i]["stop_id"] == to_stop
            ),
            None,
        )
        if to_idx is None:
            continue

        from_row = trip_rows[from_idx]
        from_time = from_row.get(
            "departure_time") or from_row.get("arrival_time")
        from_secs = _to_secs(from_time)

        if target_secs is None:
            score = from_secs
        else:
            score = abs(from_secs - target_secs)

        key = (score, from_secs, trip_id)
        if best_key is None or key < best_key:
            best_key = key
            chosen_trip_id = trip_id
            chosen_direction = from_row.get("direction")
            chosen_slice = trip_rows[from_idx: to_idx + 1]

    if not chosen_trip_id or not chosen_slice:
        raise HTTPException(
            status_code=404,
            detail="No matching trip found for this route between the given stops.",
        )

    return {
        "route_id": route_id,
        "trip_id": chosen_trip_id,
        "direction": chosen_direction,
        "from_stop": from_stop,
        "to_stop": to_stop,
        "stops": [
            {
                "stop_id": row["stop_id"],
                "stop_name": row["stop_name"],
                "arrival_time": str(row["arrival_time"]),
                "departure_time": str(row["departure_time"]),
                "stop_sequence": row["stop_sequence"],
            }
            for row in chosen_slice
        ],
    }
