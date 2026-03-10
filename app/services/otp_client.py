"""
OpenTripPlanner (OTP) API client.

Plans journeys via the OTP v2 GraphQL endpoint and translates OTP's response
format into the application's own leg/journey representation.

Configuration (app/config.py / .env):
  OTP_URL     Base URL of the OTP server   (default: http://localhost:8080)
  OTP_ROUTER  Router name for OTP v1 REST fallback (default: default)

OTP v2 GraphQL endpoint used by default:
  POST {OTP_URL}/otp/gtfs/v1

OTP v1 REST fallback (used when v2 is unreachable):
  GET  {OTP_URL}/otp/routers/{OTP_ROUTER}/plan
"""

import logging
from datetime import date, time
from typing import Any, Dict, List, Optional

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
  $transferPenalty: Int!
) {
  plan(
    from: { lat: $fromLat, lon: $fromLon }
    to:   { lat: $toLat,   lon: $toLon   }
    date: $date
    time: $time
    numItineraries: $numItineraries
    transportModes: [{ mode: TRANSIT }, { mode: WALK }]
    walkSpeed: $walkSpeed
    transferPenalty: $transferPenalty
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
          departure
        }
        to {
          name
          lat
          lon
          stop { gtfsId }
          arrival
        }
        route {
          gtfsId
          shortName
          longName
          agency { name }
        }
        trip { directionId }
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
            legs.append({
                "mode": "walk",
                "from_stop": from_place.get("name", ""),
                "to_stop": to_place.get("name", ""),
                "distance_km": round((otp_leg.get("distance") or 0) / 1000.0, 3),
            })

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
                direction_id = int((trip.get("directionId") or otp_leg.get("directionId")) or 0)
            except (TypeError, ValueError):
                direction_id = 0
            direction = "inbound" if direction_id == 1 else "outbound"

            # Times: prefer stop-level timestamps, fall back to leg-level
            dep_ms = from_place.get("departure") or otp_leg.get("startTime") or 0
            arr_ms = to_place.get("arrival") or otp_leg.get("endTime") or 0

            legs.append({
                "mode": "bus",
                "route_id": route_id,
                "route_name": route_name,
                "operator": operator,
                "direction": direction,
                "origin_stop_id": origin_stop_id,
                "origin_stop_name": from_place.get("name", ""),
                "destination_stop_id": dest_stop_id,
                "destination_stop_name": to_place.get("name", ""),
                "departure_time": _ms_to_time_str(dep_ms),
                "arrival_time": _ms_to_time_str(arr_ms),
            })

    return legs


def _itineraries_to_journeys(itineraries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert a list of OTP itineraries into our journey list."""
    journeys: List[Dict[str, Any]] = []

    for itinerary in itineraries:
        legs = _parse_legs(itinerary.get("legs") or [])
        if not legs:
            continue

        bus_count = sum(1 for leg in legs if leg.get("mode") == "bus")
        if bus_count == 0:
            continue  # skip walk-only itineraries

        journey_type = (
            "direct" if bus_count == 1
            else "transfer" if bus_count == 2
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
) -> List[Dict[str, Any]]:
    """
    Plan a journey using OpenTripPlanner.

    Tries the OTP v2 GraphQL endpoint first.  If the v2 endpoint is
    unreachable (connection error or timeout) it falls back to the OTP v1
    REST API.  Any other HTTP error is re-raised so the caller can handle it.

    Returns a list of journey dicts compatible with the application's
    existing response format (same structure as the previous custom router).

    Raises:
        httpx.ConnectError / httpx.TimeoutException  — OTP unreachable
        httpx.HTTPStatusError                        — OTP HTTP error
    """
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
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        # --- OTP v2 GraphQL ---
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
            return _itineraries_to_journeys(plan.get("itineraries") or [])

        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            logger.warning(
                "OTP v2 endpoint unreachable (%s), falling back to OTP v1 REST.", exc
            )
            return await _plan_via_v1(
                client,
                origin_lat, origin_lon,
                dest_lat, dest_lon,
                dep_time, dep_date,
                num_itineraries, preference, walking_speed,
            )
