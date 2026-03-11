#!/usr/bin/env python3
"""
Export the transport database to a GTFS ZIP file for OpenTripPlanner.

Usage:
    python scripts/export_gtfs.py [output_path]

The output path defaults to gtfs_export.zip in the current directory.
The resulting ZIP can be placed in OTP's data directory (typically
/var/otp/graphs/default/) and OTP will load it on startup.

Data sanitisation applied during export:
  - Stops: latitude/longitude validated against the NW Lancashire bounding box;
    stops outside this range or with non-numeric coordinates are skipped.
  - Stop names: leading/trailing whitespace stripped; empty names replaced with
    the stop_id as a fallback.
  - Route/trip/stop IDs: whitespace stripped; characters problematic in GTFS
    (commas, raw whitespace) replaced with underscores.
  - Times: validated to be parseable as HH:MM:SS; rows with unparseable times
    are skipped so OTP does not reject the feed.
  - Service calendar: derived from the days_of_week bitmask stored in the
    timetables table (Mon=1 ... Sun=64).  Date ranges use valid_from/valid_until
    when present; otherwise a two-year window from today is used.
  - Trips with fewer than two valid stop_times rows are discarded (OTP
    requires at least an origin and a destination per trip).
"""

import csv
import io
import logging
import os
import re
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

import psycopg2

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_CONFIG: Dict[str, Any] = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "user": os.environ.get("DB_USER", "transport"),
    "password": os.environ.get("DB_PASSWORD", "transport_dev"),
    "dbname": os.environ.get("DB_NAME", "transport_db"),
}

# Approximate bounding box for North West Lancashire
LAT_MIN, LAT_MAX = 53.0, 55.0
LON_MIN, LON_MAX = -3.5, -2.0

# Fallback service date window when valid_from/valid_until is NULL
DEFAULT_START_DATE: date = date.today()
DEFAULT_END_DATE: date = date.today() + timedelta(days=730)

# GTFS route_type for bus (https://gtfs.org/documentation/schedule/reference/#routestxt)
ROUTE_TYPE_BUS = 3

# Agency metadata used in agency.txt
AGENCY_URL = "https://www.lancashire.gov.uk/"
AGENCY_TIMEZONE = "Europe/London"

# Feed publisher metadata for feed_info.txt
FEED_PUBLISHER_NAME = "SCC200 Transport"
FEED_PUBLISHER_URL = "https://www.lancashire.gov.uk/"
FEED_LANG = "en"
FEED_ID = "scc200"


# ---------------------------------------------------------------------------
# Sanitisation helpers
# ---------------------------------------------------------------------------

def sanitize_id(value: Any) -> Optional[str]:
    """Return a GTFS-safe ID string, or None if the value is unusable."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    # Replace characters that are problematic in GTFS CSV (commas, raw whitespace)
    return re.sub(r"[\s,]+", "_", s)


def sanitize_name(value: Any, fallback: str = "") -> str:
    """Return a clean display-name string."""
    if value is None:
        return fallback
    s = str(value).strip()
    return s if s else fallback


def sanitize_lat_lon(
    lat: Any, lon: Any
) -> Tuple[Optional[float], Optional[float]]:
    """
    Validate and return (lat, lon) rounded to 6 d.p., or (None, None) when:
      - the values cannot be parsed as floats, or
      - the coordinates lie outside the NW Lancashire bounding box.
    """
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return None, None
    if not (LAT_MIN <= lat_f <= LAT_MAX) or not (LON_MIN <= lon_f <= LON_MAX):
        return None, None
    return round(lat_f, 6), round(lon_f, 6)


def sanitize_time(value: Any) -> Optional[str]:
    """
    Return a GTFS-valid HH:MM:SS time string.

    GTFS allows hours >= 24 for services that run past midnight (e.g. 25:30:00
    for a departure 1.5 hours after midnight).  This function accepts such
    values as long as minutes and seconds are in range.  Returns None if the
    value cannot be parsed.
    """
    if value is None:
        return None
    # Handle Python timedelta (sometimes returned by psycopg2 for TIME columns)
    if hasattr(value, "total_seconds"):
        total = int(value.total_seconds())
        h, rem = divmod(total, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    s = str(value).strip()
    parts = s.split(":")
    if len(parts) == 2:
        parts.append("00")
    if len(parts) != 3:
        return None
    try:
        h, m, sec = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None
    if h < 0 or not (0 <= m < 60) or not (0 <= sec < 60):
        return None
    return f"{h:02d}:{m:02d}:{sec:02d}"


def gtfs_date(d: Any) -> str:
    """Format a date as YYYYMMDD for GTFS."""
    if isinstance(d, (date, datetime)):
        return d.strftime("%Y%m%d")
    if d is None:
        return DEFAULT_START_DATE.strftime("%Y%m%d")
    # Accept string dates in YYYY-MM-DD or YYYYMMDD form
    return str(d).replace("-", "")[:8]


def bitmask_to_calendar_days(bitmask: int) -> Dict[str, int]:
    """Convert a days_of_week bitmask (Mon=1 ... Sun=64) to GTFS day columns."""
    return {
        "monday":    1 if bitmask & 1 else 0,
        "tuesday":   1 if bitmask & 2 else 0,
        "wednesday": 1 if bitmask & 4 else 0,
        "thursday":  1 if bitmask & 8 else 0,
        "friday":    1 if bitmask & 16 else 0,
        "saturday":  1 if bitmask & 32 else 0,
        "sunday":    1 if bitmask & 64 else 0,
    }


def make_service_id(bitmask: int, start: Any, end: Any) -> str:
    """Create a deterministic, human-readable GTFS service_id."""
    s = gtfs_date(start) if start else gtfs_date(DEFAULT_START_DATE)
    e = gtfs_date(end) if end else gtfs_date(DEFAULT_END_DATE)
    return f"svc_{bitmask}_{s}_{e}"


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------

def build_feed_info() -> List[Dict[str, str]]:
    """Return a single feed_info.txt row for OTP feed identification."""
    return [
        {
            "feed_publisher_name": FEED_PUBLISHER_NAME,
            "feed_publisher_url": FEED_PUBLISHER_URL,
            "feed_lang": FEED_LANG,
            "feed_id": FEED_ID,
            "feed_start_date": gtfs_date(DEFAULT_START_DATE),
            "feed_end_date": gtfs_date(DEFAULT_END_DATE),
        }
    ]


def fetch_agencies(conn) -> List[Dict[str, str]]:
    """Return one agency row per distinct operator in the routes table."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT operator FROM routes "
            "WHERE operator IS NOT NULL AND operator <> '' AND active = TRUE"
        )
        rows = cur.fetchall()
    return [
        {
            "agency_id": sanitize_id(r[0]) or r[0],
            "agency_name": sanitize_name(r[0], fallback=r[0]),
            "agency_url": AGENCY_URL,
            "agency_timezone": AGENCY_TIMEZONE,
        }
        for r in rows
        if r[0]
    ]


def fetch_stops(conn) -> Tuple[List[Dict], Set[str]]:
    """
    Return (stops_rows, valid_stop_ids).

    Only stops with valid coordinates within the NW Lancashire bounding box
    are included.  The valid_stop_ids set is used downstream to filter
    stop_times rows for stops that don't appear in stops.txt.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stop_id, stop_name, latitude, longitude "
            "FROM stops WHERE active = TRUE"
        )
        rows = cur.fetchall()

    result: List[Dict] = []
    valid_ids: Set[str] = set()
    skipped = 0

    for raw_id, raw_name, raw_lat, raw_lon in rows:
        sid = sanitize_id(raw_id)
        if not sid:
            skipped += 1
            continue
        clean_lat, clean_lon = sanitize_lat_lon(raw_lat, raw_lon)
        if clean_lat is None:
            log.warning(
                "Stop %s skipped — coordinates (%.5f, %.5f) outside NW Lancashire bounds.",
                sid, raw_lat or 0, raw_lon or 0,
            )
            skipped += 1
            continue
        result.append({
            "stop_id": sid,
            "stop_name": sanitize_name(raw_name, fallback=sid),
            "stop_lat": clean_lat,
            "stop_lon": clean_lon,
        })
        valid_ids.add(sid)

    if skipped:
        log.info("Skipped %d stops with invalid or out-of-bounds data.", skipped)
    log.info("Exported %d stops.", len(result))
    return result, valid_ids


def fetch_routes(conn) -> List[Dict]:
    """Return GTFS routes.txt rows for all active routes."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT route_id, route_name, operator FROM routes WHERE active = TRUE"
        )
        rows = cur.fetchall()

    result: List[Dict] = []
    for raw_id, raw_name, raw_operator in rows:
        rid = sanitize_id(raw_id)
        if not rid:
            continue
        result.append({
            "route_id": rid,
            "agency_id": sanitize_id(raw_operator) or "UNKNOWN",
            "route_short_name": sanitize_name(raw_name, fallback=rid),
            "route_long_name": "",
            "route_type": ROUTE_TYPE_BUS,
        })
    log.info("Exported %d routes.", len(result))
    return result


def fetch_trips_stop_times_calendar(
    conn, valid_stop_ids: Set[str]
) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """
    Query timetables and return (trips, stop_times, calendar_rows).

    - Rows referencing unknown stops are discarded.
    - Rows with unparseable times are discarded.
    - Trips with fewer than 2 valid stop_times are discarded (OTP needs at
      least an origin and a destination).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                t.route_id, t.trip_id, t.stop_id,
                t.arrival_time, t.departure_time,
                t.stop_sequence, t.direction,
                t.days_of_week, t.valid_from, t.valid_until
            FROM timetables t
            JOIN routes r ON r.route_id = t.route_id AND r.active = TRUE
            ORDER BY t.trip_id, t.stop_sequence
            """
        )
        rows = cur.fetchall()

    trips_dict: Dict[str, Dict] = {}
    calendars: Dict[str, Dict] = {}
    # trip_id -> list of stop_time rows (validated)
    st_by_trip: Dict[str, List[Dict]] = defaultdict(list)

    skipped_invalid = 0
    skipped_unknown_stop = 0

    for (
        raw_route, raw_trip, raw_stop,
        raw_arr, raw_dep,
        raw_seq, raw_dir,
        raw_bitmask, valid_from, valid_until,
    ) in rows:
        rid = sanitize_id(raw_route)
        tid = sanitize_id(raw_trip)
        sid = sanitize_id(raw_stop)

        if not rid or not tid or not sid:
            skipped_invalid += 1
            continue
        if sid not in valid_stop_ids:
            skipped_unknown_stop += 1
            continue

        arr = sanitize_time(raw_arr)
        dep = sanitize_time(raw_dep)
        if arr is None or dep is None:
            log.debug("Trip %s stop %s — invalid time(s); skipped.", tid, sid)
            skipped_invalid += 1
            continue

        try:
            seq = int(raw_seq)
        except (TypeError, ValueError):
            skipped_invalid += 1
            continue

        # Build service calendar entry
        bitmask = int(raw_bitmask) if raw_bitmask is not None else 127
        svc_id = make_service_id(bitmask, valid_from, valid_until)
        if svc_id not in calendars:
            calendars[svc_id] = {
                "service_id": svc_id,
                **bitmask_to_calendar_days(bitmask),
                "start_date": gtfs_date(valid_from) if valid_from else gtfs_date(DEFAULT_START_DATE),
                "end_date":   gtfs_date(valid_until) if valid_until else gtfs_date(DEFAULT_END_DATE),
            }

        # Register trip
        if tid not in trips_dict:
            direction_id = 1 if (raw_dir or "").strip().lower() == "inbound" else 0
            trips_dict[tid] = {
                "route_id": rid,
                "service_id": svc_id,
                "trip_id": tid,
                "trip_headsign": "",
                "direction_id": direction_id,
            }

        st_by_trip[tid].append({
            "trip_id": tid,
            "arrival_time": arr,
            "departure_time": dep,
            "stop_id": sid,
            "stop_sequence": seq,
        })

    if skipped_invalid:
        log.info("Skipped %d stop_time rows with invalid data.", skipped_invalid)
    if skipped_unknown_stop:
        log.info(
            "Skipped %d stop_time rows referencing stops absent from stops.txt.",
            skipped_unknown_stop,
        )

    # Discard trips with fewer than 2 valid stop_times
    valid_trips: List[Dict] = []
    stop_times: List[Dict] = []
    discarded_trips = 0

    for tid, rows_for_trip in st_by_trip.items():
        if len(rows_for_trip) < 2:
            discarded_trips += 1
            continue
        valid_trips.append(trips_dict[tid])
        stop_times.extend(rows_for_trip)

    if discarded_trips:
        log.info("Discarded %d trips with fewer than 2 stop_time rows.", discarded_trips)

    log.info(
        "Exported %d trips, %d stop_times, %d service calendar entries.",
        len(valid_trips), len(stop_times), len(calendars),
    )
    return valid_trips, stop_times, list(calendars.values())


# ---------------------------------------------------------------------------
# GTFS ZIP writer
# ---------------------------------------------------------------------------

def write_gtfs_zip(output_path: str, files: Dict[str, List[Dict]]) -> None:
    """Write GTFS files into a ZIP archive at output_path."""
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for filename, rows in files.items():
            if not rows:
                log.warning("%s — no data rows; writing empty file.", filename)
                zf.writestr(filename, "")
                continue
            buf = io.StringIO()
            writer = csv.DictWriter(
                buf,
                fieldnames=list(rows[0].keys()),
                lineterminator="\n",
            )
            writer.writeheader()
            writer.writerows(rows)
            zf.writestr(filename, buf.getvalue())
            log.info("  %-20s  %d rows", filename, len(rows))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def export_gtfs(output_path: str = "gtfs_export.zip") -> None:
    """Connect to the database and write a sanitised GTFS ZIP to output_path."""
    log.info("Connecting to database...")
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        log.info("Fetching agencies...")
        agencies = fetch_agencies(conn)

        log.info("Fetching stops...")
        stops, valid_stop_ids = fetch_stops(conn)

        log.info("Fetching routes...")
        routes = fetch_routes(conn)

        log.info("Fetching trips and stop times...")
        trips, stop_times, calendar = fetch_trips_stop_times_calendar(conn, valid_stop_ids)

        log.info("Writing GTFS ZIP to %s...", output_path)
        write_gtfs_zip(
            output_path,
            {
                "feed_info.txt":  build_feed_info(),
                "agency.txt":     agencies,
                "stops.txt":      stops,
                "routes.txt":     routes,
                "trips.txt":      trips,
                "stop_times.txt": stop_times,
                "calendar.txt":   calendar,
            },
        )
        log.info("GTFS export complete: %s", output_path)
    finally:
        conn.close()


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "gtfs_export.zip"
    export_gtfs(out)
