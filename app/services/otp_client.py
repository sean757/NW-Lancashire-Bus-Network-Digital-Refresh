"""
OpenTripPlanner (OTP) API client.

Plans journeys via the OTP v2 GraphQL endpoint and translates OTP's response
format into the application's own leg/journey representation.

Configuration (app/config.py / .env):
  OTP_URL     Base URL of the OTP server   (default: http://localhost:9090)
  OTP_ROUTER  Router name for OTP v1 REST fallback (default: default)

OTP v2 GraphQL endpoint used by default:
  POST {OTP_URL}/otp/gtfs/v1

OTP v1 REST fallback (used when v2 is unreachable):
  GET  {OTP_URL}/otp/routers/{OTP_ROUTER}/plan
"""

import logging
from datetime import date, time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# OTP v2 GraphQL query
# ---------------------------------------------------------------------------

_PLAN_QUERY = """
query PlanJourney(
  $fromLat: Float!, $fromLon: Float!,
  $toLat: Float!, $toLon: Float!,
  $date: String!, $time: String!,
  $numItineraries: Int!,
  $walkSpeed: Float!,
  $transferPenalty: Int!,
  $arriveBy: Boolean!,
  $searchWindow: Long
) {
  plan(
    from: { lat: $fromLat, lon: $fromLon }
    to:   { lat: $toLat,   lon: $toLon   }
    date: $date
    time: $time
    numItineraries: $numItineraries
    searchWindow: $searchWindow
    transportModes: [{ mode: TRANSIT }, { mode: WALK }]
    walkSpeed: $walkSpeed
    transferPenalty: $transferPenalty
    arriveBy: $arriveBy
  ) {
    itineraries {
      duration
      startTime
      endTime
      numberOfTransfers
      legs {
        mode
        startTime
        endTime
        duration
        distance
        from {
          name
          lat
          lon
          stop { gtfsId }
        }
        to {
          name
          lat
          lon
          stop { gtfsId }
        }
        route {
          gtfsId
          shortName
          longName
          agency { name }
        }
        trip { directionId }
        legGeometry { length points }
      }
    }
  }
}
"""

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

_TRANSIT_MODES = frozenset({
    "BUS", "TRAM", "RAIL", "SUBWAY", "FERRY", "GONDOLA", "FUNICULAR", "CABLE_CAR"
})


def _strip_feed_prefix(otp_id: Optional[str]) -> str:
    """Remove the OTP feed-prefix from an ID (e.g. '1:ABC' → 'ABC')."""
    if not otp_id:
        return ""
    return otp_id.split(":", 1)[1] if ":" in otp_id else otp_id


def _ms_to_time_str(epoch_ms: Optional[int]) -> str:
    """Convert epoch-milliseconds to a local-time HH:MM:SS string."""
    if not epoch_ms:
        return ""
    from datetime import datetime
    dt = datetime.fromtimestamp(epoch_ms / 1000.0)
    return dt.strftime("%H:%M:%S")


def _walk_speed_mps(preference: str) -> float:
    """Map a walking-speed preference label to metres per second."""
    return {"slow": 1.0, "fast": 2.0}.get(preference, 1.4)


def _transfer_penalty(preference: str) -> int:
    """
    Return an OTP transfer-penalty (seconds) that encourages fewest changes.
    'least-changes' adds a 5-minute cost per transfer to steer OTP toward
    itineraries with fewer board/alight events.
    """
    return 300 if preference == "least-changes" else 0


def _decode_polyline(encoded: str) -> List[List[float]]:
    """
    Decode a Google Encoded Polyline string into a list of [lat, lon] pairs.

    OTP returns walking-leg geometry as an encoded polyline in
    ``legGeometry.points``.  This decoder converts that string into plain
    coordinate pairs so the frontend can draw the walking path directly.
    """
    if not encoded:
        return []
    coords: List[List[float]] = []
    index = 0
    lat = 0
    lng = 0
    n = len(encoded)
    while index < n:
        result, shift = 0, 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlat = ~(result >> 1) if result & 1 else result >> 1
        lat += dlat

        result, shift = 0, 0
        while True:
            b = ord(encoded[index]) - 63
            index += 1
            result |= (b & 0x1F) << shift
            shift += 5
            if b < 0x20:
                break
        dlng = ~(result >> 1) if result & 1 else result >> 1
        lng += dlng

        coords.append([round(lat / 1e5, 6), round(lng / 1e5, 6)])
    return coords


# ---------------------------------------------------------------------------
# Response parsing (shared between v1 and v2)
# ---------------------------------------------------------------------------

def _parse_legs(otp_legs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert a list of OTP leg dicts into our application leg format."""
    legs: List[Dict[str, Any]] = []

    for otp_leg in otp_legs:
        mode = (otp_leg.get("mode") or "").upper()

        if mode == "WALK":
            from_place = otp_leg.get("from") or {}
            to_place = otp_leg.get("to") or {}
            leg_geom = otp_leg.get("legGeometry") or {}
            encoded_pts = leg_geom.get("points") or ""
            waypoints = _decode_polyline(encoded_pts)
            dep_ms = otp_leg.get("startTime") or 0
            arr_ms = otp_leg.get("endTime") or 0
            walk_leg: Dict[str, Any] = {
                "mode": "walk",
                "from_stop": from_place.get("name", ""),
                "to_stop": to_place.get("name", ""),
                "distance_km": round((otp_leg.get("distance") or 0) / 1000.0, 3),
                "departure_time": _ms_to_time_str(dep_ms),
                "arrival_time": _ms_to_time_str(arr_ms),
                "from_lat": from_place.get("lat"),
                "from_lon": from_place.get("lon"),
                "to_lat": to_place.get("lat"),
                "to_lon": to_place.get("lon"),
            }
            if waypoints:
                walk_leg["waypoints"] = waypoints
            legs.append(walk_leg)

        elif mode in _TRANSIT_MODES:
            from_place = otp_leg.get("from") or {}
            to_place = otp_leg.get("to") or {}
            route = otp_leg.get("route") or {}
            trip = otp_leg.get("trip") or {}

            # OTP v2: stop IDs are nested under from.stop.gtfsId
            # OTP v1: stop IDs are directly on from.stopId
            origin_stop_id = _strip_feed_prefix(
                ((from_place.get("stop") or {}).get("gtfsId"))
                or from_place.get("stopId")
            )
            dest_stop_id = _strip_feed_prefix(
                ((to_place.get("stop") or {}).get("gtfsId"))
                or to_place.get("stopId")
            )

            # OTP v2: routeId under route.gtfsId; OTP v1: top-level routeId
            route_id = _strip_feed_prefix(
                route.get("gtfsId") or otp_leg.get("routeId")
            )
            route_name = (
                route.get("shortName")
                or route.get("longName")
                or otp_leg.get("route")           # v1 "route" field
                or otp_leg.get("routeShortName")  # v1 alternative
                or route_id
            )
            operator = (
                (route.get("agency") or {}).get("name")
                or otp_leg.get("agencyName", "")
            )

            try:
                direction_id = int(
                    (trip.get("directionId") or otp_leg.get("directionId")) or 0)
            except (TypeError, ValueError):
                direction_id = 0
            direction = "inbound" if direction_id == 1 else "outbound"

            # Times: use leg-level epoch-ms timestamps (Long in OTP v2,
            # int in v1).  In OTP 2.4+, Place.departure/arrival became LegTime
            # objects — using the leg-level startTime/endTime avoids that
            # schema change entirely.
            dep_ms = otp_leg.get("startTime") or 0
            arr_ms = otp_leg.get("endTime") or 0

            # Extract OTP's own route geometry for this bus leg so the
            # frontend can draw it accurately.  OTP returns the precise path
            # for the trip it selected; this avoids mismatches when the
            # route_waypoints table contains a different variant.
            leg_geom = otp_leg.get("legGeometry") or {}
            otp_waypoints = _decode_polyline(leg_geom.get("points") or "")

            if mode == "RAIL":
                mode_label = "rail"
            elif mode == "TRAM":
                mode_label = "tram"
            elif mode == "SUBWAY":
                mode_label = "subway"
            elif mode == "FERRY":
                mode_label = "ferry"
            else:
                mode_label = "bus"

            transit_leg: Dict[str, Any] = {
                "mode": mode_label,
                "route_id": route_id,
                "route_name": route_name,
                "operator": operator,
                "direction": direction,
                "origin_stop_id": origin_stop_id,
                "origin_stop_name": from_place.get("name", ""),
                "origin_lat": from_place.get("lat"),
                "origin_lon": from_place.get("lon"),
                "destination_stop_id": dest_stop_id,
                "destination_stop_name": to_place.get("name", ""),
                "destination_lat": to_place.get("lat"),
                "destination_lon": to_place.get("lon"),
                "departure_time": _ms_to_time_str(dep_ms),
                "arrival_time": _ms_to_time_str(arr_ms),
            }
            if otp_waypoints:
                transit_leg["otp_waypoints"] = otp_waypoints
            legs.append(transit_leg)

    return legs


def _itineraries_to_journeys(itineraries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert a list of OTP itineraries into our journey list."""
    journeys: List[Dict[str, Any]] = []

    for itinerary in itineraries:
        legs = _parse_legs(itinerary.get("legs") or [])
        if not legs:
            continue

        # Count all transit legs (bus, rail, tram, ferry, etc.) — not just bus —
        # so that rail-only or tram-only itineraries are not incorrectly filtered.
        transit_legs = [leg for leg in legs if (
            leg.get("mode") or "").lower() != "walk"]
        if not transit_legs:
            continue  # skip walk-only itineraries

        transit_count = len(transit_legs)
        journey_type = (
            "direct" if transit_count == 1
            else "transfer" if transit_count == 2
            else "multi-transfer"
        )
        journeys.append({"type": journey_type, "legs": legs})

    return journeys


# ---------------------------------------------------------------------------
# OTP v1 REST fallback
# ---------------------------------------------------------------------------

async def _plan_via_v1(
    client: httpx.AsyncClient,
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    dep_time: time,
    dep_date: date,
    num_itineraries: int,
    preference: str,
    walking_speed: str,
    arrive_by: bool = False,
) -> List[Dict[str, Any]]:
    """Call the OTP v1 REST planner and return our journey list."""
    url = f"{settings.otp_url}/otp/routers/{settings.otp_router}/plan"
    params = {
        "fromPlace": f"{origin_lat},{origin_lon}",
        "toPlace": f"{dest_lat},{dest_lon}",
        "date": dep_date.strftime("%Y-%m-%d"),
        "time": dep_time.strftime("%H:%M:%S"),
        "mode": "TRANSIT,WALK",
        "numItineraries": num_itineraries,
        "maxWalkDistance": 1000,
        "walkSpeed": _walk_speed_mps(walking_speed),
        "transferPenalty": _transfer_penalty(preference),
        "arriveBy": "true" if arrive_by else "false",
    }
    resp = await client.get(url, params=params, timeout=30.0)
    resp.raise_for_status()
    data = resp.json()
    plan = data.get("plan") or {}
    return _itineraries_to_journeys(plan.get("itineraries") or [])


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def plan_journey(
    origin_lat: float,
    origin_lon: float,
    dest_lat: float,
    dest_lon: float,
    dep_time: time,
    dep_date: date,
    num_itineraries: int = 10,
    preference: str = "fastest",
    walking_speed: str = "medium",
    arrive_by: bool = False,
) -> List[Dict[str, Any]]:
    """
    Plan a journey using OpenTripPlanner.

    Tries the OTP v2 GraphQL endpoint first.  If the v2 endpoint is
    unreachable (connection error or timeout) it falls back to the OTP v1
    REST API.  Any other HTTP error is re-raised so the caller can handle it.

    Returns a list of journey dicts compatible with the application's
    existing response format (same structure as the previous custom router).

    When ``arrive_by=True`` returns fewer than 3 journeys a supplementary
    search is performed for itineraries that arrive up to 30 minutes after
    the requested arrival time.  Those journeys are tagged with
    ``arrives_late=True`` and ``late_by_mins=<N>`` so the frontend can
    display an appropriate warning.

    Raises:
        httpx.ConnectError / httpx.TimeoutException  — OTP unreachable
        httpx.HTTPStatusError                        — OTP HTTP error
    """
    from datetime import datetime as _datetime, timedelta as _timedelta

    graphql_url = f"{settings.otp_url}/otp/gtfs/v1"
    variables = {
        "fromLat": origin_lat,
        "fromLon": origin_lon,
        "toLat": dest_lat,
        "toLon": dest_lon,
        "date": dep_date.strftime("%Y-%m-%d"),
        "time": dep_time.strftime("%H:%M:%S"),
        "numItineraries": num_itineraries,
        "walkSpeed": _walk_speed_mps(walking_speed),
        "transferPenalty": _transfer_penalty(preference),
        "arriveBy": arrive_by,
        "searchWindow": 3600,  # 1-hour window so OTP finds more alternatives
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        # --- OTP v2 GraphQL ---
        # use_v2 tracks which transport succeeded so the late-journey supplement
        # can reuse the same endpoint rather than redundantly retrying the other.
        use_v2 = True
        try:
            resp = await client.post(
                graphql_url,
                json={"query": _PLAN_QUERY, "variables": variables},
            )
            resp.raise_for_status()
            data = resp.json()

            # GraphQL errors are returned as HTTP 200 with an "errors" key
            if data.get("errors"):
                logger.warning("OTP v2 GraphQL errors: %s", data["errors"])

            plan = (data.get("data") or {}).get("plan") or {}
            journeys = _itineraries_to_journeys(plan.get("itineraries") or [])

        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            use_v2 = False
            logger.warning(
                "OTP v2 endpoint unreachable (%s), falling back to OTP v1 REST.", exc
            )
            journeys = await _plan_via_v1(
                client,
                origin_lat, origin_lon,
                dest_lat, dest_lon,
                dep_time, dep_date,
                num_itineraries, preference, walking_speed,
                arrive_by=arrive_by,
            )

        # --- Supplement with "just late" journeys when arrive_by yields few results ---
        # If the arrive-by search found fewer than 3 itineraries, also search for
        # journeys arriving up to 30 minutes after the requested arrival time and
        # tag them so the frontend can warn the user.
        if arrive_by and len(journeys) < 3:
            late_threshold_mins = 30
            target_dt = _datetime.combine(dep_date, dep_time)
            late_dt = target_dt + _timedelta(minutes=late_threshold_mins)
            late_time = late_dt.time()
            late_date = late_dt.date()

            try:
                if use_v2:
                    late_vars = {
                        **variables,
                        "date": late_date.strftime("%Y-%m-%d"),
                        "time": late_time.strftime("%H:%M:%S"),
                    }
                    resp2 = await client.post(
                        graphql_url,
                        json={"query": _PLAN_QUERY, "variables": late_vars},
                    )
                    resp2.raise_for_status()
                    data2 = resp2.json()
                    plan2 = (data2.get("data") or {}).get("plan") or {}
                    late_candidates = _itineraries_to_journeys(
                        plan2.get("itineraries") or [])
                else:
                    late_candidates = await _plan_via_v1(
                        client,
                        origin_lat, origin_lon,
                        dest_lat, dest_lon,
                        late_time, late_date,
                        num_itineraries, preference, walking_speed,
                        arrive_by=True,
                    )

                target_total_mins = dep_time.hour * 60 + dep_time.minute
                next_day = late_dt.date() > dep_date

                for j in late_candidates:
                    legs = j.get("legs") or []
                    last_leg = legs[-1] if legs else None
                    if not last_leg:
                        continue
                    arr_str = last_leg.get("arrival_time", "")  # "HH:MM:SS"
                    if not arr_str:
                        continue
                    parts = arr_str.split(":")
                    try:
                        arr_mins = int(parts[0]) * 60 + int(parts[1])
                    except (IndexError, ValueError):
                        continue

                    late_by = arr_mins - target_total_mins
                    # Handle midnight wraparound: arrival next day but target was same-day
                    if late_by < 0 and next_day:
                        late_by += 24 * 60

                    if 0 < late_by <= late_threshold_mins:
                        j["arrives_late"] = True
                        j["late_by_mins"] = late_by
                        journeys.append(j)

            except (httpx.ConnectError, httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                logger.warning(
                    "Late-journey supplement search failed: %s", exc)

        return journeys
