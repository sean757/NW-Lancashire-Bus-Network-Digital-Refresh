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
3.  Project the live vehicle's GPS position onto the trip's route segments
    (consecutive pairs of stops) to find a *continuous* position — both
    which segment the vehicle is on and how far along it (0.0–1.0).
4.  Linearly interpolate the scheduled time at that fractional position.
5.  Delay = (current wall-clock time) − (interpolated scheduled time).

This continuous-interpolation approach avoids the coarse errors of discrete
stop-snapping, where a bus 90% between two stops would be "snapped" back to
the earlier stop and accumulate a large false delay.

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

# Maximum distance (km) from the route line for a vehicle to be considered "on" it.
_MAX_ROUTE_DISTANCE_KM = 0.5

# ---------------------------------------------------------------------------
# Vehicle → trip_id mapping cache.
#
# Each SIRI vehicle carries a DatedVehicleJourneyRef (DVJR) that uniquely
# identifies the journey within a (line, direction) on a given day.  While
# the DVJR numbering differs from the TransXChange VehicleJourneyCode stored
# in our DB, we can resolve the mapping once by matching the vehicle's origin
# stop + aimed departure time against the timetable.  The resolved trip_id
# is cached so subsequent delay requests for the same vehicle are instant
# and exact.
#
# Cache key: (line_ref, direction_ref, dvjr)  →  (trip_id, route_id) or None
# ---------------------------------------------------------------------------
_dvjr_trip_cache: Dict[tuple, Optional[tuple]] = {}
# The vehicle-cache generation count when _dvjr_trip_cache was last rebuilt.
_dvjr_cache_generation: Optional[datetime] = None


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


def _find_vehicles_on_route(
    route_name: str,
    direction: Optional[str] = None,
) -> list[dict]:
    """Return cached vehicles whose line_ref or line_name matches the route.

    The SIRI feed uses operator-specific line references (e.g. "40", "X1")
    which should match the route_name from our GTFS data.  We compare
    case-insensitively and strip whitespace.

    When *direction* is provided, only vehicles whose ``direction_ref`` matches
    are returned.  This prevents a vehicle travelling in the opposite direction
    from being mis-identified as the one serving this leg.
    """
    if not route_name:
        return []
    target = route_name.strip().upper()
    target_dir = (direction or "").strip().lower()
    matches = []
    for v in vehicle_cache.vehicles:
        # Skip vehicles with missing or clearly invalid GPS coordinates
        # (the SIRI feed sometimes returns 0,0 for vehicles whose GPS is unavailable).
        vlat = v.get("latitude")
        vlon = v.get("longitude")
        if vlat is None or vlon is None or (vlat == 0.0 and vlon == 0.0):
            continue
        line = (v.get("line_ref") or v.get("line_name") or "").strip().upper()
        if line != target:
            continue
        # Filter by direction when available
        if target_dir:
            v_dir = (v.get("direction_ref") or "").strip().lower()
            if v_dir and v_dir != target_dir:
                continue
        matches.append(v)
    return matches


def _parse_aimed_departure_secs(vehicle: dict) -> Optional[int]:
    """Extract OriginAimedDepartureTime from a vehicle dict as seconds since midnight.

    The SIRI feed provides ISO-8601 timestamps like ``2026-03-18T18:48:00+00:00``.
    Returns seconds since midnight (local time portion) or None.
    """
    raw = (vehicle.get("origin_aimed_departure") or "").strip()
    if not raw:
        return None
    # Extract the time portion: look for HH:MM:SS after 'T'
    try:
        if "T" in raw:
            time_part = raw.split("T")[1]
            # Strip timezone offset (+00:00, Z, etc.)
            for sep in ("+", "-", "Z"):
                if sep in time_part and time_part.index(sep) > 0:
                    time_part = time_part[:time_part.index(sep)]
                    break
            parts = time_part.split(":")
            h, m = int(parts[0]), int(parts[1])
            s = int(parts[2]) if len(parts) > 2 else 0
            return h * 3600 + m * 60 + s
    except (ValueError, IndexError):
        pass
    return None


# ---------------------------------------------------------------------------
#  DVJR → trip_id resolution
# ---------------------------------------------------------------------------

def _resolve_dvjr_to_trip(vehicle: dict) -> Optional[tuple]:
    """Resolve a vehicle's DatedVehicleJourneyRef to a (trip_id, route_id).

    Uses the vehicle's origin stop + aimed departure + direction + line to
    find the matching timetable trip.  Returns ``(trip_id, route_id)`` or
    ``None``.
    """
    aimed_secs = _parse_aimed_departure_secs(vehicle)
    if aimed_secs is None:
        return None

    v_origin = (vehicle.get("origin_ref") or "").strip()
    if not v_origin:
        return None

    line = (vehicle.get("line_ref") or "").strip().upper()
    direction = (vehicle.get("direction_ref") or "").strip().lower()

    # Search all routes with this line name for a trip departing from
    # v_origin at aimed_secs.
    best = None
    best_diff = float("inf")

    for rid, rinfo in route_cache.routes.items():
        if (rinfo.get("route_name") or "").strip().upper() != line:
            continue
        dep_key = (rid, direction, v_origin)
        for sched_dep, trip_id in route_cache.stop_departures.get(dep_key, []):
            diff = abs(sched_dep - aimed_secs)
            if diff < best_diff:
                best_diff = diff
                best = (trip_id, rid)

    # Accept only if within 5 minutes of the aimed departure
    if best and best_diff <= 300:
        return best
    return None


def _refresh_dvjr_cache() -> None:
    """Rebuild the DVJR → trip_id cache if the vehicle cache has been updated."""
    global _dvjr_trip_cache, _dvjr_cache_generation

    vc_updated = vehicle_cache.last_updated
    if vc_updated == _dvjr_cache_generation:
        return  # still fresh

    new_cache: Dict[tuple, Optional[tuple]] = {}
    for v in vehicle_cache.vehicles:
        dvjr = (v.get("dated_vehicle_journey_ref") or "").strip()
        if not dvjr:
            continue
        line = (v.get("line_ref") or "").strip().upper()
        direction = (v.get("direction_ref") or "").strip().lower()
        key = (line, direction, dvjr)
        if key in new_cache:
            continue  # already resolved
        new_cache[key] = _resolve_dvjr_to_trip(v)

    _dvjr_trip_cache = new_cache
    _dvjr_cache_generation = vc_updated
    logger.debug(
        "DVJR cache rebuilt: %d entries, %d resolved",
        len(new_cache),
        sum(1 for v in new_cache.values() if v is not None),
    )


def _get_trip_for_vehicle(vehicle: dict) -> Optional[tuple]:
    """Look up the (trip_id, route_id) for a vehicle via its DVJR.

    Returns ``(trip_id, route_id)`` or ``None`` if the DVJR is missing or
    could not be resolved.
    """
    dvjr = (vehicle.get("dated_vehicle_journey_ref") or "").strip()
    if not dvjr:
        return None
    line = (vehicle.get("line_ref") or "").strip().upper()
    direction = (vehicle.get("direction_ref") or "").strip().lower()
    return _dvjr_trip_cache.get((line, direction, dvjr))


def _find_vehicle_on_trip_route(
    vlat: float,
    vlon: float,
    trip_id: str,
) -> Optional[tuple]:
    """Locate a vehicle along a trip's stop sequence using segment projection.

    Instead of snapping to the single nearest stop (which loses all inter-stop
    progress), this projects the vehicle onto each consecutive pair of stops
    and picks the segment where the perpendicular distance is smallest.

    Returns ``(seg_index, fraction, perp_dist_km)`` or ``None``:

    - *seg_index*: index into ``trip_stops_sorted[trip_id]`` for the segment
      start stop.
    - *fraction*: 0.0 → at stop[seg_index], 1.0 → at stop[seg_index+1].
    - *perp_dist_km*: approximate perpendicular distance from the vehicle to
      the route segment (quality metric — lower is better).
    """
    trip_stops = route_cache.trip_stops_sorted.get(trip_id, [])
    if len(trip_stops) < 2:
        return None

    best = None
    for i in range(len(trip_stops) - 1):
        sid_a = trip_stops[i][0]
        sid_b = trip_stops[i + 1][0]
        ca = route_cache.stop_coords.get(sid_a)
        cb = route_cache.stop_coords.get(sid_b)
        if not ca or not cb:
            continue

        da = _haversine_km(vlat, vlon, ca[0], ca[1])
        db = _haversine_km(vlat, vlon, cb[0], cb[1])
        dab = _haversine_km(ca[0], ca[1], cb[0], cb[1])

        if dab < 0.001:  # two stops at essentially the same location
            frac = 0.0
            perp = da
        else:
            # Projection fraction via the cosine rule.
            frac = max(0.0, min(1.0,
                                (da ** 2 + dab ** 2 - db ** 2) / (2 * dab ** 2)))
            # Perpendicular distance via Heron's formula.
            s = (da + db + dab) / 2.0
            area_sq = s * (s - da) * (s - db) * (s - dab)
            perp = (2.0 * math.sqrt(max(0.0, area_sq)) / dab)

        if best is None or perp < best[2]:
            best = (i, frac, perp)

    return best


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


def _interpolate_scheduled_time(
    trip_id: str,
    seg_index: int,
    fraction: float,
) -> Optional[float]:
    """Linearly interpolate the scheduled time at a fractional route position.

    Uses the *departure* time of the segment's start stop and the *arrival*
    time of the segment's end stop.

    Returns seconds since midnight, or ``None``.
    """
    trip_stops = route_cache.trip_stops_sorted.get(trip_id, [])
    if seg_index + 1 >= len(trip_stops):
        return None

    # Each entry is (stop_id, seq, arr_secs, dep_secs)
    dep_a = trip_stops[seg_index][3]      # departure from segment start
    arr_b = trip_stops[seg_index + 1][2]  # arrival at segment end

    if dep_a is None or arr_b is None:
        return None

    # Guard against backward time (can happen with timing-point-only data)
    if arr_b < dep_a:
        arr_b = dep_a

    return dep_a + fraction * (arr_b - dep_a)


def _propagate_route_delay(
    vehicles: list[dict],
    route_id: str,
    origin_stop_id: str,
    dep_secs: int,
    current_secs: int,
) -> Optional[Dict[str, Any]]:
    """Propagate delay from the nearest vehicle on the same route.

    When no vehicle is matched to the user's specific trip (e.g. the trip
    hasn't started yet), this finds the closest vehicle on the same route
    and computes its current delay.  That delay is returned as a
    ``route_estimate`` — a reasonable proxy for future departures on the
    same line.
    """
    best_vehicle = None
    best_diff = float("inf")
    best_trip = None

    for vehicle in vehicles:
        resolved = _get_trip_for_vehicle(vehicle)
        if not resolved:
            continue
        v_trip_id, v_route_id = resolved
        if v_route_id != route_id:
            continue
        stop_info = route_cache.trip_stop_seq.get(
            (v_trip_id, origin_stop_id))
        if not stop_info:
            continue
        trip_dep = stop_info[2]
        diff = abs(trip_dep - dep_secs)
        if diff < best_diff:
            best_diff = diff
            best_vehicle = vehicle
            best_trip = v_trip_id

    if not best_vehicle or best_diff > 3600:  # within 1 hour
        return None

    trip_stops = route_cache.trip_stops_sorted.get(best_trip, [])
    if not trip_stops or len(trip_stops) < 2:
        return None

    vlat = best_vehicle.get("latitude")
    vlon = best_vehicle.get("longitude")
    if vlat is None or vlon is None:
        return None

    segment = _find_vehicle_on_trip_route(vlat, vlon, best_trip)
    if segment is None:
        return None
    seg_index, fraction, perp_dist = segment
    if perp_dist > _MAX_ROUTE_DISTANCE_KM:
        return None

    sched_time = _interpolate_scheduled_time(best_trip, seg_index, fraction)
    if sched_time is None:
        return None

    delay_secs = current_secs - sched_time
    delay_mins = round(delay_secs / 60)
    status = "on_time" if abs(delay_mins) <= 1 else "delayed"

    return {
        "delay_mins": delay_mins,
        "source": "route_estimate",
        "status": status,
        "vehicle_id": best_vehicle.get("vehicle_id"),
    }


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

    Matching strategy
    -----------------
    1. **DVJR-based (preferred):** Look up each vehicle's
       ``DatedVehicleJourneyRef`` in the pre-built DVJR→trip_id cache.
       If the resolved trip_id matches the leg's trip, use that vehicle
       directly — no fuzzy matching needed.
    2. **Fuzzy fallback:** If no DVJR match is found, fall back to the
       aimed-departure-time + origin/destination membership heuristic.
    """
    if not route_id or not origin_stop_id:
        return None

    # Ensure the DVJR cache is up-to-date with the latest vehicle refresh.
    _refresh_dvjr_cache()

    # 1. Find live vehicles on this route (filtered by direction)
    vehicles = _find_vehicles_on_route(route_name, direction)
    if not vehicles:
        return None

    # 3. Convert departure time to seconds since midnight
    dep_secs = _time_str_to_secs(departure_time)
    if dep_secs is None:
        return None

    # 4. Current wall-clock time in seconds since midnight
    now = datetime.now()
    current_secs = now.hour * 3600 + now.minute * 60 + now.second

    # 5. Route stop set for membership checks
    ordered_stops = route_cache.get_stops_for_route(route_id, direction)
    route_stop_ids = {s[0] for s in ordered_stops} if ordered_stops else set()

    # ------------------------------------------------------------------
    # 6. Try DVJR-based exact matching first.
    #
    # Instead of finding a trip then matching a vehicle to it, we start
    # from each vehicle's DVJR-resolved trip and check whether that trip
    # serves the user's origin stop near the requested departure time.
    # This avoids the problem of duplicate trip_ids for the same physical
    # departure (different day-of-week variants).
    # ------------------------------------------------------------------
    _DEP_MATCH_WINDOW = 900   # 15 minutes
    _NOT_STARTED_WINDOW = 2700  # 45 minutes lookahead for trips not yet started

    dvjr_vehicle = None
    dvjr_trip_id = None
    dvjr_aimed_diff = float("inf")

    for vehicle in vehicles:
        resolved = _get_trip_for_vehicle(vehicle)
        if not resolved:
            continue
        v_trip_id, v_route_id = resolved
        if v_route_id != route_id:
            continue
        # Check this trip serves the user's origin stop near dep_secs
        stop_info = route_cache.trip_stop_seq.get(
            (v_trip_id, origin_stop_id))
        if not stop_info:
            continue
        trip_dep_secs = stop_info[2]  # dep_secs at user's stop
        diff = abs(trip_dep_secs - dep_secs)
        if diff > _DEP_MATCH_WINDOW:
            continue
        if diff < dvjr_aimed_diff:
            dvjr_aimed_diff = diff
            dvjr_vehicle = vehicle
            dvjr_trip_id = v_trip_id

    # ------------------------------------------------------------------
    # 7. Fuzzy fallback if no DVJR match
    # ------------------------------------------------------------------
    if dvjr_vehicle is None:
        # Need a trip to project against — find one the traditional way.
        trip_id = _find_matching_trip(
            route_id, direction, origin_stop_id, dep_secs, travel_date)
        if not trip_id:
            return None

        trip_stops = route_cache.trip_stops_sorted.get(trip_id, [])
        if not trip_stops or len(trip_stops) < 2:
            return None

        # Trip-not-started guard
        _user_stop_info = route_cache.trip_stop_seq.get(
            (trip_id, origin_stop_id))
        if _user_stop_info:
            if current_secs < _user_stop_info[2] - _NOT_STARTED_WINDOW:
                return None

        best_aimed_diff = float("inf")
        for vehicle in vehicles:
            aimed_secs = _parse_aimed_departure_secs(vehicle)
            v_origin = (vehicle.get("origin_ref") or "").strip()
            if aimed_secs is not None:
                match_dep = trip_stops[0][3]
                if v_origin:
                    info = route_cache.trip_stop_seq.get(
                        (trip_id, v_origin))
                    if info:
                        match_dep = info[2]
                aimed_diff = abs(aimed_secs - match_dep)
                if aimed_diff > _DEP_MATCH_WINDOW:
                    continue
            else:
                aimed_diff = float("inf")

            v_dest = (vehicle.get("destination_ref") or "").strip()
            if v_origin or v_dest:
                o_ok = v_origin in route_stop_ids if v_origin else False
                d_ok = v_dest in route_stop_ids if v_dest else False
                if not o_ok and not d_ok:
                    continue

            if aimed_diff < best_aimed_diff or (dvjr_vehicle is None and aimed_diff <= best_aimed_diff):
                best_aimed_diff = aimed_diff
                dvjr_vehicle = vehicle
                dvjr_trip_id = trip_id
    else:
        trip_id = dvjr_trip_id

    if dvjr_vehicle is None or dvjr_trip_id is None:
        # ---- Route-level delay propagation fallback ----
        # No specific trip/vehicle match, but we have live vehicles on
        # this route.  Compute the delay for the nearest vehicle on the
        # same route and propagate it as an estimate for this departure.
        return _propagate_route_delay(
            vehicles, route_id, origin_stop_id, dep_secs, current_secs)

    # Trip-not-started guard (DVJR path)
    _user_stop_info = route_cache.trip_stop_seq.get(
        (dvjr_trip_id, origin_stop_id))
    if _user_stop_info and current_secs < _user_stop_info[2] - _NOT_STARTED_WINDOW:
        return None

    trip_stops = route_cache.trip_stops_sorted.get(dvjr_trip_id, [])
    if not trip_stops or len(trip_stops) < 2:
        return None

    # ------------------------------------------------------------------
    # 8. Compute delay via segment projection + time interpolation
    # ------------------------------------------------------------------
    vlat = dvjr_vehicle.get("latitude")
    vlon = dvjr_vehicle.get("longitude")
    if vlat is None or vlon is None:
        return None

    segment = _find_vehicle_on_trip_route(vlat, vlon, dvjr_trip_id)
    if segment is None:
        return None
    seg_index, fraction, perp_dist = segment
    if perp_dist > _MAX_ROUTE_DISTANCE_KM:
        return None

    sched_time = _interpolate_scheduled_time(dvjr_trip_id, seg_index, fraction)
    if sched_time is None:
        return None

    delay_secs = current_secs - sched_time
    delay_mins = round(delay_secs / 60)

    status = "on_time" if abs(delay_mins) <= 1 else "delayed"

    return {
        "delay_mins": delay_mins,
        "source": "live_position",
        "status": status,
        "vehicle_id": dvjr_vehicle.get("vehicle_id"),
    }


# =====================================================================
#  Rail delay estimation (Darwin departure board)
# =====================================================================

async def estimate_rail_leg_delay(
    origin_crs: Optional[str],
    departure_time: Optional[str],
    destination_crs: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Estimate the delay for a rail leg using the Darwin departure board.

    Unlike buses (where delay must be inferred from GPS positions), rail
    delays are reported *exactly* by the Darwin feed.  We simply fetch the
    departure board for the origin station, find the service whose
    scheduled departure time matches, and read its estimated time.

    Returns ``{"delay_mins": int, "source": "darwin", ...}`` or ``None``.
    """
    if not origin_crs or not departure_time:
        return None

    from app.services.rail_feed import rail_feed

    board = await rail_feed.get_departures(origin_crs)
    if board.get("error") or not board.get("train_services"):
        return None

    # Parse the OTP departure time (HH:MM:SS or HH:MM) to HH:MM for matching.
    dep_parts = departure_time.strip().split(":")
    if len(dep_parts) < 2:
        return None
    dep_hhmm = f"{int(dep_parts[0]):02d}:{int(dep_parts[1]):02d}"

    best_match = None
    best_score = -1  # higher is better

    for svc in board["train_services"]:
        sched = svc.get("scheduled_departure") or ""
        if sched != dep_hhmm:
            continue

        # Matched on departure time.  If we also know the destination CRS,
        # use it to disambiguate when multiple services depart at the same
        # time (common at major stations like Preston).
        score = 1
        if destination_crs:
            dests = {d["crs"].upper() for d in (svc.get("destinations") or [])}
            # Also check calling points for through-services
            cp_crss = {
                cp["crs"].upper()
                for cp in (svc.get("calling_points") or [])
                if cp.get("crs")
            }
            if destination_crs.upper() in dests:
                score = 10  # exact destination match
            elif destination_crs.upper() in cp_crss:
                score = 5   # destination is a calling point

        if score > best_score:
            best_score = score
            best_match = svc

    if best_match is None:
        return None

    delay = best_match.get("departure_delay_minutes")
    is_cancelled = best_match.get("is_cancelled", False)

    result: Dict[str, Any] = {
        "source": "darwin",
        "service_id": best_match.get("service_id"),
        "operator": best_match.get("operator"),
        "platform": best_match.get("platform"),
        "is_cancelled": is_cancelled,
    }

    if is_cancelled:
        result["delay_mins"] = None
        result["status"] = "cancelled"
        result["cancel_reason"] = best_match.get("cancel_reason")
    elif delay is not None:
        result["delay_mins"] = delay
        result["status"] = "on_time" if delay == 0 else "delayed"
        if best_match.get("delay_reason"):
            result["delay_reason"] = best_match["delay_reason"]
        result["estimated_departure"] = best_match.get("estimated_departure")
    else:
        # "Delayed" with no specific time
        result["delay_mins"] = None
        result["status"] = "delayed"
        result["estimated_departure"] = best_match.get("estimated_departure")

    # Include calling-point delay propagation if available.
    if destination_crs and best_match.get("calling_points"):
        for cp in best_match["calling_points"]:
            if (cp.get("crs") or "").upper() == destination_crs.upper():
                result["destination_delay_minutes"] = cp.get("delay_minutes")
                result["destination_estimated_time"] = cp.get("estimated_time")
                break

    return result
