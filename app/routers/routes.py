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
    direction: str = Query(default="outbound", description="Route direction (outbound/inbound)"),
    from_stop: str | None = Query(default=None, description="Origin stop ID — only return waypoints from this stop onward"),
    to_stop: str | None = Query(default=None, description="Destination stop ID — only return waypoints up to this stop"),
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
        # Find the sequence numbers for the two stops, then return the slice.
        query = """
            WITH stop_seqs AS (
                SELECT
                    MIN(CASE WHEN stop_id = :from_stop THEN sequence END) AS from_seq,
                    MIN(CASE WHEN stop_id = :to_stop   THEN sequence END) AS to_seq
                FROM route_waypoints
                WHERE route_id = :route_id
                  AND direction = :direction
            )
            SELECT w.latitude, w.longitude
            FROM route_waypoints w
            CROSS JOIN stop_seqs
            WHERE w.route_id  = :route_id
              AND w.direction  = :direction
              AND w.sequence  >= COALESCE(stop_seqs.from_seq, 0)
              AND w.sequence  <= COALESCE(stop_seqs.to_seq,   2147483647)
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
            SELECT latitude, longitude
            FROM route_waypoints
            WHERE route_id = :route_id
              AND direction = :direction
            ORDER BY sequence
        """
        result = await db.execute(
            text(query),
            {"route_id": route_id, "direction": direction},
        )

    rows = result.mappings().all()
    return [{"lat": row["latitude"], "lon": row["longitude"]} for row in rows]
