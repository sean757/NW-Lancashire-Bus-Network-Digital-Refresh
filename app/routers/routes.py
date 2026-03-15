"""
Routes router — endpoints for querying bus route geometry.
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from app.database import get_db

router = APIRouter()


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
