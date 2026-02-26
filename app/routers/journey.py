"""
Journey planner router — plan journeys between two stops.
Supports direct routes, single-transfer journeys, and time-aware filtering.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from pydantic import BaseModel
from datetime import date, time, datetime
from typing import Optional
from app.database import get_db
from app.services.route_cache import route_cache

router = APIRouter()


class JourneyRequest(BaseModel):
    origin_stop_id: Optional[str] = None
    origin_lat: Optional[float] = None
    origin_lon: Optional[float] = None
    destination_stop_id: Optional[str] = None
    destination_lat: Optional[float] = None
    destination_lon: Optional[float] = None
    departure_time: Optional[str] = None  # HH:MM format
    departure_date: Optional[str] = None  # YYYY-MM-DD format


def get_day_bitmask(d: date) -> int:
    """Convert a date to the days_of_week bitmask. Monday=1, Tuesday=2, etc."""
    # weekday() returns 0=Monday, 1=Tuesday, ...
    return 1 << d.weekday()


async def find_nearby_stop(lat: float, lon: float, db: AsyncSession) -> Optional[str]:
    """Find the nearest stop to given coordinates."""
    query = """
        SELECT stop_id,
            (6371 * acos(
                cos(radians(:lat)) * cos(radians(latitude)) *
                cos(radians(longitude) - radians(:lon)) +
                sin(radians(:lat)) * sin(radians(latitude))
            )) AS distance_km
        FROM stops
        WHERE active = TRUE
        ORDER BY distance_km
        LIMIT 1
    """
    result = await db.execute(text(query), {"lat": lat, "lon": lon})
    row = result.mappings().first()
    return row["stop_id"] if row else None


async def get_timetabled_journeys(
    route_id: str,
    direction: str,
    origin_stop_id: str,
    dest_stop_id: str,
    dep_time: time,
    dep_date: date,
    db: AsyncSession,
    limit: int = 5,
):
    """Get the next departures for a specific route between two stops, filtered by day and time."""
    day_mask = get_day_bitmask(dep_date)

    query = """
        SELECT
            t_orig.trip_id,
            t_orig.departure_time AS origin_departure,
            t_dest.arrival_time AS destination_arrival,
            t_orig.days_of_week,
            t_orig.valid_from,
            t_orig.valid_until
        FROM timetables t_orig
        JOIN timetables t_dest
            ON t_orig.route_id = t_dest.route_id
            AND t_orig.trip_id = t_dest.trip_id
            AND t_orig.direction = t_dest.direction
        WHERE t_orig.route_id = :route_id
          AND t_orig.direction = :direction
          AND t_orig.stop_id = :origin_stop
          AND t_dest.stop_id = :dest_stop
          AND t_orig.stop_sequence < t_dest.stop_sequence
          AND t_orig.departure_time >= :dep_time
          AND (t_orig.days_of_week & :day_mask) > 0
          AND (t_orig.valid_from IS NULL OR t_orig.valid_from <= :dep_date)
          AND (t_orig.valid_until IS NULL OR t_orig.valid_until >= :dep_date)
        ORDER BY t_orig.departure_time
        LIMIT :limit
    """

    result = await db.execute(text(query), {
        "route_id": route_id,
        "direction": direction,
        "origin_stop": origin_stop_id,
        "dest_stop": dest_stop_id,
        "dep_time": dep_time,
        "day_mask": day_mask,
        "dep_date": dep_date,
        "limit": limit,
    })

    return [dict(row) for row in result.mappings().all()]


@router.post("/plan")
async def plan_journey(req: JourneyRequest, db: AsyncSession = Depends(get_db)):
    """Plan a journey between two stops. Tries direct routes first, then transfers."""

    # Resolve origin
    origin = req.origin_stop_id
    if not origin and req.origin_lat and req.origin_lon:
        origin = await find_nearby_stop(req.origin_lat, req.origin_lon, db)
    if not origin:
        raise HTTPException(
            status_code=400, detail="Origin stop required (stop_id or lat/lon)")

    # Resolve destination
    destination = req.destination_stop_id
    if not destination and req.destination_lat and req.destination_lon:
        destination = await find_nearby_stop(req.destination_lat, req.destination_lon, db)
    if not destination:
        raise HTTPException(
            status_code=400, detail="Destination stop required (stop_id or lat/lon)")

    # Parse time and date
    if req.departure_time:
        dep_time = datetime.strptime(req.departure_time, "%H:%M").time()
    else:
        dep_time = datetime.now().time()

    if req.departure_date:
        # Try multiple date formats
        dep_date = None
        for date_format in ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"]:
            try:
                dep_date = datetime.strptime(
                    req.departure_date, date_format).date()
                break
            except ValueError:
                continue
        if dep_date is None:
            raise HTTPException(
                status_code=400, detail="Invalid departure_date format. Use YYYY-MM-DD, DD/MM/YYYY, or DD-MM-YYYY")
    else:
        dep_date = date.today()

    # Get stop names for the response
    origin_name_result = await db.execute(
        text("SELECT stop_name FROM stops WHERE stop_id = :sid"), {
            "sid": origin}
    )
    dest_name_result = await db.execute(
        text("SELECT stop_name FROM stops WHERE stop_id = :sid"), {
            "sid": destination}
    )
    origin_name = (origin_name_result.scalar() or origin)
    dest_name = (dest_name_result.scalar() or destination)

    journeys = []

    # ==========================================
    # 1. Try direct routes
    # ==========================================
    direct_routes = route_cache.get_common_routes(origin, destination)

    for route in direct_routes:
        timetabled = await get_timetabled_journeys(
            route["route_id"],
            route["direction"],
            origin,
            destination,
            dep_time,
            dep_date,
            db,
        )

        for trip in timetabled:
            journeys.append({
                "type": "direct",
                "legs": [
                    {
                        "route_id": route["route_id"],
                        "route_name": route["route_name"],
                        "operator": route["operator"],
                        "direction": route["direction"],
                        "origin_stop_id": origin,
                        "origin_stop_name": origin_name,
                        "destination_stop_id": destination,
                        "destination_stop_name": dest_name,
                        "departure_time": str(trip["origin_departure"]),
                        "arrival_time": str(trip["destination_arrival"]),
                    }
                ],
            })

    # ==========================================
    # 2. Try single-transfer routes if no direct
    # ==========================================
    if not journeys:
        transfer_options = route_cache.find_transfer_routes(
            origin, destination)

        for transfer in transfer_options[:10]:  # Limit results
            # Get timetable for leg 1
            leg1_trips = await get_timetabled_journeys(
                transfer["leg1_route_id"],
                transfer["leg1_direction"],
                origin,
                transfer["leg1_alight_stop"],
                dep_time,
                dep_date,
                db,
                limit=3,
            )

            for leg1 in leg1_trips:
                # Use leg1 arrival to find leg2 departures
                leg1_arrival = leg1["destination_arrival"]

                leg2_trips = await get_timetabled_journeys(
                    transfer["leg2_route_id"],
                    transfer["leg2_direction"],
                    transfer["transfer_stop"],
                    destination,
                    leg1_arrival,
                    dep_date,
                    db,
                    limit=2,
                )

                # Get transfer stop names
                alight_name_result = await db.execute(
                    text("SELECT stop_name FROM stops WHERE stop_id = :sid"),
                    {"sid": transfer["leg1_alight_stop"]}
                )
                board_name_result = await db.execute(
                    text("SELECT stop_name FROM stops WHERE stop_id = :sid"),
                    {"sid": transfer["transfer_stop"]}
                )
                alight_name = (alight_name_result.scalar()
                               or transfer["leg1_alight_stop"])
                board_name = (board_name_result.scalar()
                              or transfer["transfer_stop"])

                for leg2 in leg2_trips:
                    journeys.append({
                        "type": "transfer",
                        "legs": [
                            {
                                "mode": "bus",
                                "route_id": transfer["leg1_route_id"],
                                "route_name": transfer["leg1_route_name"],
                                "direction": transfer["leg1_direction"],
                                "origin_stop_id": origin,
                                "origin_stop_name": origin_name,
                                "destination_stop_id": transfer["leg1_alight_stop"],
                                "destination_stop_name": alight_name,
                                "departure_time": str(leg1["origin_departure"]),
                                "arrival_time": str(leg1["destination_arrival"]),
                            },
                            {
                                "mode": "walk",
                                "distance_km": transfer["walk_distance_km"],
                                "from_stop": alight_name,
                                "to_stop": board_name,
                            },
                            {
                                "mode": "bus",
                                "route_id": transfer["leg2_route_id"],
                                "route_name": transfer["leg2_route_name"],
                                "direction": transfer["leg2_direction"],
                                "origin_stop_id": transfer["transfer_stop"],
                                "origin_stop_name": board_name,
                                "destination_stop_id": destination,
                                "destination_stop_name": dest_name,
                                "departure_time": str(leg2["origin_departure"]),
                                "arrival_time": str(leg2["destination_arrival"]),
                            },
                        ],
                    })

    # Sort by earliest arrival
    def get_arrival(j):
        last_bus_leg = [l for l in j["legs"]
                        if l.get("mode", "bus") == "bus"][-1]
        return last_bus_leg["arrival_time"]

    journeys.sort(key=get_arrival)

    return {
        "origin": {"stop_id": origin, "name": origin_name},
        "destination": {"stop_id": destination, "name": dest_name},
        "departure_date": dep_date.isoformat(),
        "departure_time": dep_time.strftime("%H:%M"),
        "results_count": len(journeys),
        "journeys": journeys[:10],  # Return top 10
    }
