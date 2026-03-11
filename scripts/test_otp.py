#!/usr/bin/env python3
"""
Standalone OTP connectivity and routing test.

Tests the OpenTripPlanner server directly — no database or FastAPI backend
required.  This is useful for debugging OTP in isolation before coupling it
to the rest of the application.

Usage:
    python scripts/test_otp.py [--url http://localhost:9090]

The script runs four checks:
  1. Server reachability  — confirms OTP is up and returning a valid response.
  2. Feed metadata        — lists every GTFS feed loaded by OTP.
  3. Route inventory      — lists all transit routes known to OTP.
  4. Journey planning     — plans a sample journey between two well-known
                            Lancaster city-centre stops and prints the result.

If OTP is unreachable the script prints troubleshooting advice and exits with
a non-zero code.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request


# ---------------------------------------------------------------------------
# Default configuration
# ---------------------------------------------------------------------------

DEFAULT_OTP_URL = "http://localhost:9090"

# Sample journey: Lancaster Bus Station Underpass → Common Garden Street
# These are real NaPTAN ATCOCodes for Lancaster city-centre stops.
# The script resolves them via the OTP stops API so the coordinates stay
# accurate even if the database is refreshed.
SAMPLE_FROM_NAME = "Lancaster Bus Station"
SAMPLE_TO_NAME = "Common Garden Street"

# Fallback coordinates used when the stops cannot be resolved via the API.
# These are approximate Lancaster city-centre positions.
SAMPLE_FROM_LAT = 54.0479
SAMPLE_FROM_LON = -2.7996
SAMPLE_TO_LAT = 54.0476
SAMPLE_TO_LON = -2.8017

# GraphQL query for journey planning (OTP v2)
_PLAN_QUERY = """
query TestPlan(
  $fromLat: Float!, $fromLon: Float!,
  $toLat: Float!, $toLon: Float!,
  $date: String!, $time: String!
) {
  plan(
    from: { lat: $fromLat, lon: $fromLon }
    to:   { lat: $toLat,   lon: $toLon   }
    date: $date
    time: $time
    numItineraries: 5
    transportModes: [{ mode: TRANSIT }, { mode: WALK }]
  ) {
    itineraries {
      duration
      legs {
        mode
        from { name }
        to   { name }
        route { shortName agency { name } }
      }
    }
  }
}
"""

# GraphQL query to list all routes in OTP
_ROUTES_QUERY = """
{
  routes {
    gtfsId
    shortName
    longName
    agency { name }
    mode
  }
}
"""

# GraphQL query to list all feeds
_FEEDS_QUERY = """
{
  feeds {
    feedId
    agencies { name }
  }
}
"""


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post_graphql(url: str, query: str, variables: dict = None) -> dict:
    """Send a GraphQL POST request and return the parsed JSON body."""
    payload = {"query": query}
    if variables:
        payload["variables"] = variables

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_json(url: str) -> dict:
    """GET a URL and return the parsed JSON body."""
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------

def check_server(otp_url: str) -> bool:
    """Check 1: confirm OTP is reachable."""
    print("\n── Check 1: Server reachability ─────────────────────────────")
    graphql_url = f"{otp_url}/otp/gtfs/v1"
    try:
        # A minimal introspection query — always succeeds if OTP is up.
        result = _post_graphql(graphql_url, "{ __typename }")
        if result.get("data"):
            print(f"  ✓  OTP is reachable at {graphql_url}")
            return True
        print(f"  ✗  OTP responded but returned unexpected data: {result}")
        return False
    except urllib.error.URLError as exc:
        print(f"  ✗  Cannot reach OTP at {graphql_url}: {exc.reason}")
        print()
        print("  Troubleshooting:")
        print("    • Confirm OTP is running:  java -Xmx4G -jar otp-*.jar --load otp-data --port 9090")
        print("    • The graph build step must have completed successfully.")
        print("    • Check OTP_URL is set correctly (default: http://localhost:9090).")
        print("    • OTP takes ~30 seconds to start after the graph is loaded.")
        return False
    except Exception as exc:
        print(f"  ✗  Unexpected error: {exc}")
        return False


def check_feeds(otp_url: str) -> bool:
    """Check 2: list GTFS feeds loaded by OTP."""
    print("\n── Check 2: GTFS feeds ──────────────────────────────────────")
    graphql_url = f"{otp_url}/otp/gtfs/v1"
    try:
        result = _post_graphql(graphql_url, _FEEDS_QUERY)
        feeds = (result.get("data") or {}).get("feeds") or []
        if not feeds:
            print("  ✗  No GTFS feeds loaded.")
            print()
            print("  Troubleshooting:")
            print("    • Ensure gtfs_export.zip is present in the otp-data/ directory.")
            print("    • Re-run: python scripts/ingest_timetables.py")
            print("    • Re-run: python scripts/export_gtfs.py otp-data/gtfs_export.zip")
            print("    • Rebuild the OTP graph: java -Xmx4G -jar otp-*.jar --build --save otp-data")
            return False

        for feed in feeds:
            agencies = ", ".join(a["name"] for a in (feed.get("agencies") or []))
            print(f"  ✓  Feed '{feed.get('feedId')}' — agencies: {agencies or '(none)'}")
        return True

    except Exception as exc:
        print(f"  ✗  Could not query feeds: {exc}")
        return False


def check_routes(otp_url: str) -> bool:
    """Check 3: list all transit routes known to OTP."""
    print("\n── Check 3: Route inventory ─────────────────────────────────")
    graphql_url = f"{otp_url}/otp/gtfs/v1"
    try:
        result = _post_graphql(graphql_url, _ROUTES_QUERY)
        routes = (result.get("data") or {}).get("routes") or []
        if not routes:
            print("  ✗  No routes found in OTP.")
            print()
            print("  This usually means the GTFS data has no valid trips.")
            print("  Common causes:")
            print("    • trip_id values are empty or not unique (fixed in ingest_timetables.py).")
            print("    • Service calendar dates do not cover today.")
            print("    • All stop coordinates are outside OTP's OSM extract bounding box.")
            print("    • The OTP graph was built before the GTFS data was exported.")
            return False

        print(f"  ✓  {len(routes)} route(s) loaded:")
        for route in sorted(routes, key=lambda r: r.get("shortName") or ""):
            name = route.get("shortName") or route.get("longName") or "(no name)"
            agency = (route.get("agency") or {}).get("name", "")
            mode = route.get("mode", "")
            print(f"       {name:<10}  {mode:<6}  {agency}")
        return True

    except Exception as exc:
        print(f"  ✗  Could not query routes: {exc}")
        return False


def check_journey(otp_url: str, from_lat: float, from_lon: float,
                  to_lat: float, to_lon: float) -> bool:
    """Check 4: plan a sample journey and display the itineraries."""
    print("\n── Check 4: Journey planning ─────────────────────────────────")
    graphql_url = f"{otp_url}/otp/gtfs/v1"

    # Use a fixed weekday morning time so results are deterministic
    # regardless of when this script is run.
    date_str = "2026-03-11"
    time_str = "08:30:00"

    print(f"  Planning: ({from_lat}, {from_lon}) → ({to_lat}, {to_lon})")
    print(f"  Date/time: {date_str} {time_str}")

    variables = {
        "fromLat": from_lat, "fromLon": from_lon,
        "toLat": to_lat,     "toLon": to_lon,
        "date": date_str,    "time": time_str,
    }

    try:
        result = _post_graphql(graphql_url, _PLAN_QUERY, variables)
        errors = result.get("errors")
        if errors:
            print(f"  ✗  GraphQL errors: {errors}")
            return False

        itineraries = ((result.get("data") or {}).get("plan") or {}).get("itineraries") or []
        if not itineraries:
            print("  ✗  No itineraries returned.")
            print()
            print("  This can happen when:")
            print("    • No transit services run between these coordinates.")
            print("    • The service calendar does not cover the requested date.")
            print("    • The origin/destination is too far from any stop.")
            return False

        transit_count = sum(
            1 for it in itineraries
            if any(leg.get("mode") not in ("WALK", None) for leg in (it.get("legs") or []))
        )
        print(f"  ✓  {len(itineraries)} itinerary/ies returned ({transit_count} with transit):")
        for i, it in enumerate(itineraries, 1):
            legs = it.get("legs") or []
            duration_min = round((it.get("duration") or 0) / 60, 1)
            summary = " → ".join(
                (leg.get("route") or {}).get("shortName")
                or leg.get("mode", "?")
                for leg in legs
            )
            print(f"       [{i}] {duration_min} min  |  {summary}")
        return transit_count > 0

    except Exception as exc:
        print(f"  ✗  Journey planning failed: {exc}")
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Standalone OTP connectivity and routing test"
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_OTP_URL,
        help=f"Base URL of the OTP server (default: {DEFAULT_OTP_URL})",
    )
    parser.add_argument(
        "--from-lat", type=float, default=SAMPLE_FROM_LAT,
        help="Origin latitude for the sample journey",
    )
    parser.add_argument(
        "--from-lon", type=float, default=SAMPLE_FROM_LON,
        help="Origin longitude for the sample journey",
    )
    parser.add_argument(
        "--to-lat", type=float, default=SAMPLE_TO_LAT,
        help="Destination latitude for the sample journey",
    )
    parser.add_argument(
        "--to-lon", type=float, default=SAMPLE_TO_LON,
        help="Destination longitude for the sample journey",
    )
    args = parser.parse_args()

    print(f"OTP Diagnostic Tool — target: {args.url}")
    print("=" * 60)

    ok_server  = check_server(args.url)
    if not ok_server:
        sys.exit(1)

    ok_feeds   = check_feeds(args.url)
    ok_routes  = check_routes(args.url)
    ok_journey = check_journey(
        args.url,
        args.from_lat, args.from_lon,
        args.to_lat,   args.to_lon,
    )

    print("\n── Summary ───────────────────────────────────────────────────")
    results = [
        ("Server reachable",  ok_server),
        ("Feeds loaded",      ok_feeds),
        ("Routes present",    ok_routes),
        ("Transit journeys",  ok_journey),
    ]
    all_ok = True
    for label, passed in results:
        mark = "✓" if passed else "✗"
        print(f"  {mark}  {label}")
        if not passed:
            all_ok = False

    if all_ok:
        print("\n  All checks passed — OTP is working correctly.")
    else:
        print("\n  One or more checks failed.  See above for details.")
        print("  Refer to the README for setup instructions.")
        sys.exit(1)


if __name__ == "__main__":
    main()
