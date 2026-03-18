"""
Delay estimator service — derives approximate delay estimates for transit legs
by comparing live vehicle positions from the SIRI feed against scheduled
timetable data.

This implements *post-hoc annotation*: OTP plans against the static schedule,
and we augment each transit leg with a delay estimate before returning it to
the frontend.  The routing itself is not altered.

How it works
============
1.  For a given bus leg (route + direction + departure time + origin stop),
    look up active vehicles on that route from the in-memory VehicleCache.
2.  Find the closest timetable trip that serves the leg's origin stop near
    the scheduled departure time.
3.  Determine where that trip *should* be right now according to the schedule
    (i.e. which stop it should be nearest to at the current time).
4.  Determine where the live vehicle *actually* is (closest stop on the route).
5.  Convert the gap between "expected stop" and "actual stop" into an
    approximate delay in minutes using the scheduled inter-stop times.

If no live vehicle is found on the route the function returns None so the
caller can mark the leg as ``delay_source: "schedule"``.
"""

import logging
import math
from datetime import datetime, date, time as dtime
from typing import Optional, Dict, Any

from app.services.vehicle_cache import vehicle_cache
from app.services.route_cache import route_cache

logger = logging.getLogger(__name__)

# Maximum age (seconds) of a vehicle position before we consider it stale.
_MAX_VEHICLE_AGE_SECS = 300  # 5 minutes

# Maximum distance (km) to consider a vehicle as being "on" a stop.
_SNAP_RADIUS_KM = 0.5


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Return the great-circle distance in km between two points."""
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _time_str_to_secs(t: str) -> Optional[int]:
    """Convert 'HH:MM' or 'HH:MM:SS' to seconds since midnight."""
    if not t:
        return None
    parts = t.split(":")
    try:
        h, m = int(parts[0]), int(parts[1])
        s = int(parts[2]) if len(parts) > 2 else 0
        return h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        return None


def _find_vehicles_on_route(route_name: str) -> list[dict]:
    """Return cached vehicles whose line_ref or line_name matches the route.

    The SIRI feed uses operator-specific line references (e.g. "40", "X1")
    which should match the route_name from our GTFS data.  We compare
    case-insensitively and strip whitespace.
    """
    if not route_name:
        return []
    target = route_name.strip().upper()
    matches = []
    for v in vehicle_cache.vehicles:
        line = (v.get("line_ref") or v.get("line_name") or "").strip().upper()
        if line == target:
            matches.append(v)
    return matches


def _snap_vehicle_to_stop(
    vehicle_lat: float,
    vehicle_lon: float,
    route_stops: list[tuple],
) -> Optional[tuple]:
    """Find the closest stop on the route to the vehicle position.

    ``route_stops`` is a list of ``(stop_id, sequence, direction)`` tuples
    from the route cache, already filtered to the correct direction.

    Returns ``(stop_id, sequence, distance_km)`` or None if no stop is
    within ``_SNAP_RADIUS_KM``.
    """
    best = None
    for stop_id, seq, _dir in route_stops:
        coords = route_cache.stop_coords.get(stop_id)
        if not coords:
            continue
        dist = _haversine_km(vehicle_lat, vehicle_lon, coords[0], coords[1])
        if best is None or dist < best[2]:
            best = (stop_id, seq, dist)
    if best and best[2] <= _SNAP_RADIUS_KM:
        return best
    return None


def _find_matching_trip(
    route_id: str,
    direction: str,
    origin_stop_id: str,
    dep_secs: int,
    travel_date: Optional[date] = None,
) -> Optional[str]:
    """Find a timetable trip that departs from origin_stop_id closest to dep_secs.

    Searches the route_cache stop_departures index for trips on this
    route/direction/stop and returns the trip_id whose departure is nearest
    (within ±30 minutes) to the requested time.
    """
    dep_key = (route_id, direction, origin_stop_id)
    departures = route_cache.stop_departures.get(dep_key, [])
    if not departures:
        return None

    best_trip = None
    best_diff = float("inf")
    for sched_dep, trip_id in departures:
        diff = abs(sched_dep - dep_secs)
        if diff < best_diff:
            best_diff = diff
            best_trip = trip_id

    # Only accept if within 30 minutes of scheduled departure
    if best_diff > 1800:
        return None

    return best_trip


def _expected_stop_at_time(
    trip_id: str,
    current_secs: int,
) -> Optional[tuple]:
    """Determine which stop the trip should be at (or past) at ``current_secs``.

    Walks the trip's stop sequence and returns the last stop whose scheduled
    departure time is <= current_secs.

    Returns ``(stop_id, sequence, dep_secs)`` or None.
    """
    stops = route_cache.trip_stops_sorted.get(trip_id, [])
    if not stops:
        return None

    best = None
    for stop_id, seq, arr_secs, dep_secs in stops:
        if dep_secs <= current_secs:
            best = (stop_id, seq, dep_secs)
        else:
            break  # stops are sorted by sequence / time
    return best


def _estimate_delay_from_position(
    trip_id: str,
    expected_seq: int,
    expected_dep_secs: int,
    actual_seq: int,
    actual_stop_id: str,
) -> Optional[int]:
    """Estimate delay in minutes based on the gap between where the vehicle
    *should* be (expected_seq) and where it *is* (actual_seq).

    If the vehicle is behind schedule (actual_seq < expected_seq), we sum the
    scheduled inter-stop travel times between the two positions to get an
    approximate delay.

    Returns delay in minutes (>= 0), or None if we can't compute.
    """
    if actual_seq >= expected_seq:
        # Vehicle is at or ahead of the expected position — no delay (or early)
        return 0

    # The vehicle is behind schedule.  Sum scheduled travel time between
    # actual position and expected position to estimate how late it is.
    stops = route_cache.trip_stops_sorted.get(trip_id, [])
    if not stops:
        return None

    # Find times at actual and expected stops
    actual_dep = None
    expected_dep = None
    for stop_id, seq, arr_secs, dep_secs in stops:
        if seq == actual_seq:
            actual_dep = dep_secs
        if seq == expected_seq:
            expected_dep = dep_secs

    if actual_dep is not None and expected_dep is not None:
        delay_secs = expected_dep - actual_dep
        if delay_secs > 0:
            return max(1, round(delay_secs / 60))

    return None


async def estimate_leg_delay(
    route_id: str,
    route_name: str,
    direction: str,
    origin_stop_id: Optional[str],
    departure_time: Optional[str],
    travel_date: Optional[date] = None,
) -> Optional[Dict[str, Any]]:
    """Estimate the delay for a single transit leg.

    Returns a dict ``{"delay_mins": int, "source": "live_position"}``
    if a delay estimate can be derived, or ``None`` if live data is
    unavailable.
    """
    if not route_id or not origin_stop_id:
        return None

    # 1. Find live vehicles on this route
    vehicles = _find_vehicles_on_route(route_name)
    if not vehicles:
        return None

    # 2. Convert departure time to seconds since midnight
    dep_secs = _time_str_to_secs(departure_time)
    if dep_secs is None:
        return None

    # 3. Find the closest matching timetable trip
    trip_id = _find_matching_trip(
        route_id, direction, origin_stop_id, dep_secs, travel_date)
    if not trip_id:
        return None

    # 4. Get ordered stops for this route+direction
    ordered_stops = route_cache.get_stops_for_route(route_id, direction)
    if not ordered_stops:
        return None

    # 5. Determine where the trip should be right now
    now = datetime.now()
    current_secs = now.hour * 3600 + now.minute * 60 + now.second

    expected = _expected_stop_at_time(trip_id, current_secs)
    if not expected:
        # Trip hasn't started yet or has no schedule data
        return None

    # 6. Find the best matching vehicle and snap it to the nearest stop
    best_result = None
    for vehicle in vehicles:
        vlat = vehicle.get("latitude")
        vlon = vehicle.get("longitude")
        if vlat is None or vlon is None:
            continue

        snap = _snap_vehicle_to_stop(vlat, vlon, ordered_stops)
        if snap is None:
            continue

        actual_stop_id, actual_seq, dist = snap

        delay_mins = _estimate_delay_from_position(
            trip_id,
            expected[1],  # expected sequence
            expected[2],  # expected dep_secs
            actual_seq,
            actual_stop_id,
        )
        if delay_mins is not None:
            if best_result is None or delay_mins < best_result["delay_mins"]:
                best_result = {
                    "delay_mins": delay_mins,
                    "source": "live_position",
                    "vehicle_id": vehicle.get("vehicle_id"),
                }

    return best_result
