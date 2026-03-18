#!/usr/bin/env python3
"""
Ingest National Rail schedules into the transport database for OTP rail routing.

Data sources (from transport.scc.lancs.ac.uk):
- /rail/corpus   (gzip JSON) TIPLOC -> CRS mapping
- /rail/schedule (gzip NDJSON) working timetable schedules

This script:
- Builds a CRS -> NaPTAN stop_id map from the stops table (or NaPTAN fallback).
- Streams the rail schedule, selecting only trips with >=2 regional rail stops.
- Inserts rail routes and timetables into the database.
"""

import gzip
import json
import logging
import os
import re
from typing import Dict, Iterable, List, Optional, Tuple

import psycopg2
import requests
from lxml import etree
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "user": os.environ.get("DB_USER", "transport"),
    "password": os.environ.get("DB_PASSWORD", "transport_dev"),
    "dbname": os.environ.get("DB_NAME", "transport_db"),
}

RAIL_BASE_URL = os.getenv(
    "RAIL_BASE_URL", "http://transport.scc.lancs.ac.uk/rail")
CORPUS_URL = os.getenv("RAIL_CORPUS_URL", f"{RAIL_BASE_URL}/corpus")
SCHEDULE_URL = os.getenv("RAIL_SCHEDULE_URL", f"{RAIL_BASE_URL}/schedule")
NAPTAN_FULL_URL = os.getenv(
    "NAPTAN_FULL_URL",
    "https://transport.scc.lancs.ac.uk/nptg/naptan-full.xml",
)

# Regional bounding box (same as export_gtfs.py)
LAT_MIN, LAT_MAX = 53.0, 55.0
LON_MIN, LON_MAX = -3.7, -2.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sanitize_id(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return re.sub(r"[\s,]+", "_", s)


def parse_time(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    digits = re.sub(r"\D", "", s)
    if len(digits) == 3:
        digits = "0" + digits
    if len(digits) < 4:
        return None
    try:
        hh = int(digits[:2])
        mm = int(digits[2:4])
    except ValueError:
        return None
    if hh >= 24 or mm >= 60:
        return None
    return f"{hh:02d}:{mm:02d}:00"


def days_to_bitmask(days_str: Optional[str]) -> int:
    if not days_str or len(days_str) != 7:
        return 127
    bitmask = 0
    for i, ch in enumerate(days_str):
        if ch == "1":
            bitmask |= 1 << i
    return bitmask


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_corpus_tiploc_map() -> Dict[str, str]:
    log.info("Downloading rail corpus from %s", CORPUS_URL)
    resp = requests.get(CORPUS_URL, timeout=30)
    resp.raise_for_status()
    data = gzip.decompress(resp.content)
    payload = json.loads(data)

    mapping: Dict[str, str] = {}
    for row in payload.get("TIPLOCDATA", []):
        tiploc = (row.get("TIPLOC") or "").strip()
        crs = (row.get("3ALPHA") or "").strip().upper()
        if tiploc and crs:
            mapping[tiploc] = crs
    log.info("Loaded %d TIPLOC→CRS mappings.", len(mapping))
    return mapping


def load_crs_stops_from_db(conn) -> Dict[str, Tuple[str, float, float]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT stop_id, latitude, longitude, crs_code "
            "FROM stops WHERE crs_code IS NOT NULL AND crs_code <> '' AND active = TRUE"
        )
        rows = cur.fetchall()

    crs_map: Dict[str, Tuple[str, float, float]] = {}
    for stop_id, lat, lon, crs_code in rows:
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (TypeError, ValueError):
            continue
        if not (LAT_MIN <= lat_f <= LAT_MAX and LON_MIN <= lon_f <= LON_MAX):
            continue
        crs = (crs_code or "").strip().upper()
        if not crs:
            continue
        crs_map[crs] = (stop_id, lat_f, lon_f)
    return crs_map


def load_crs_stops_from_naptan(conn) -> Dict[str, Tuple[str, float, float]]:
    log.info("No CRS codes found in stops table; streaming NaPTAN for rail stops...")
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
        "Accept": "application/xml, text/xml",
    }
    response = requests.get(NAPTAN_FULL_URL, stream=True,
                            headers=headers, timeout=120)
    response.raise_for_status()
    response.raw.decode_content = True

    context = etree.iterparse(
        response.raw,
        events=("end",),
        tag="{http://www.naptan.org.uk/}StopPoint",
    )

    insert_query = """
        INSERT INTO stops (stop_id, stop_name, locality, latitude, longitude, stop_type, crs_code, active)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (stop_id) DO UPDATE SET
            stop_name = EXCLUDED.stop_name,
            locality = EXCLUDED.locality,
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            stop_type = EXCLUDED.stop_type,
            crs_code = EXCLUDED.crs_code,
            active = EXCLUDED.active;
    """

    crs_map: Dict[str, Tuple[str, float, float]] = {}
    with conn.cursor() as cur:
        for _, elem in context:
            atco_code = elem.findtext('.//{http://www.naptan.org.uk/}AtcoCode')
            name = elem.findtext('.//{http://www.naptan.org.uk/}CommonName')
            locality = elem.findtext(
                './/{http://www.naptan.org.uk/}NptgLocalityRef')
            lat = elem.findtext('.//{http://www.naptan.org.uk/}Latitude')
            lon = elem.findtext('.//{http://www.naptan.org.uk/}Longitude')
            crs_code = elem.findtext('.//{http://www.naptan.org.uk/}CrsRef')

            status = (elem.get("Status") or "").strip().lower()
            active = status != "inactive"

            crs = (crs_code or "").strip().upper()
            if not (atco_code and name and lat and lon and crs):
                elem.clear()
                while elem.getprevious() is not None:
                    del elem.getparent()[0]
                continue

            try:
                lat_f = float(lat)
                lon_f = float(lon)
            except (TypeError, ValueError):
                elem.clear()
                while elem.getprevious() is not None:
                    del elem.getparent()[0]
                continue

            if not (LAT_MIN <= lat_f <= LAT_MAX and LON_MIN <= lon_f <= LON_MAX):
                elem.clear()
                while elem.getprevious() is not None:
                    del elem.getparent()[0]
                continue

            cur.execute(
                insert_query,
                (atco_code, name, locality, lat_f, lon_f, "rail", crs, active),
            )
            crs_map[crs] = (atco_code, lat_f, lon_f)

            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]

        conn.commit()

    log.info("Loaded %d rail CRS stops from NaPTAN.", len(crs_map))
    return crs_map


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------

def clear_existing_rail_data(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM timetables WHERE route_id IN (SELECT route_id FROM routes WHERE route_type = 'rail')")
        cur.execute("DELETE FROM routes WHERE route_type = 'rail'")
    conn.commit()


def ingest_schedule(
    conn,
    tiploc_to_crs: Dict[str, str],
    crs_stops: Dict[str, Tuple[str, float, float]],
) -> None:
    log.info("Streaming rail schedule from %s", SCHEDULE_URL)

    route_cache: Dict[str, Dict[str, str]] = {}
    timetable_rows: List[Tuple] = []
    trip_count = 0

    resp = requests.get(SCHEDULE_URL, stream=True, timeout=60)
    resp.raise_for_status()
    stream = gzip.GzipFile(fileobj=resp.raw)

    for line in stream:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        sched = obj.get("JsonScheduleV1")
        if not sched:
            continue
        if (sched.get("transaction_type") or "").lower() == "delete":
            continue

        segment = sched.get("schedule_segment") or {}
        locations = segment.get("schedule_location") or []

        stop_times: List[Dict[str, str]] = []
        for loc in locations:
            tiploc = (loc.get("tiploc_code") or "").strip()
            crs = tiploc_to_crs.get(tiploc)
            if not crs:
                continue
            stop_info = crs_stops.get(crs)
            if not stop_info:
                continue

            arrival = parse_time(loc.get("public_arrival")
                                 or loc.get("arrival") or loc.get("pass"))
            departure = parse_time(loc.get("public_departure") or loc.get(
                "departure") or loc.get("pass"))

            if arrival is None and departure is None:
                continue
            if arrival is None:
                arrival = departure
            if departure is None:
                departure = arrival

            stop_times.append({
                "crs": crs,
                "stop_id": stop_info[0],
                "arrival": arrival,
                "departure": departure,
            })

        if len(stop_times) < 2:
            continue

        origin = stop_times[0]["crs"]
        dest = stop_times[-1]["crs"]

        atoc = (sched.get("atoc_code") or "ZZ").strip() or "ZZ"
        route_id = sanitize_id(f"rail_{atoc}_{origin}_{dest}")
        if not route_id:
            continue

        if route_id not in route_cache:
            route_cache[route_id] = {
                "route_id": route_id,
                "route_name": f"{origin}-{dest}",
                "operator": atoc,
                "description": "",
                "route_type": "rail",
            }

        train_uid = (sched.get("CIF_train_uid") or "")
        start_date = (sched.get("schedule_start_date") or "")
        headcode = (segment.get("signalling_id") or "")
        trip_id = sanitize_id(f"rail_{train_uid}_{start_date}_{headcode}")
        if not trip_id:
            continue

        days_bitmask = days_to_bitmask(sched.get("schedule_days_runs"))
        valid_from = sched.get("schedule_start_date")
        valid_until = sched.get("schedule_end_date")

        for seq, st in enumerate(stop_times, start=1):
            timetable_rows.append(
                (
                    route_id,
                    st["stop_id"],
                    trip_id,
                    st["arrival"],
                    st["departure"],
                    seq,
                    "outbound",
                    days_bitmask,
                    valid_from,
                    valid_until,
                )
            )

        trip_count += 1
        if len(timetable_rows) >= 5000:
            _flush_routes(conn, list(route_cache.values()))
            _flush_timetables(conn, timetable_rows)
            timetable_rows.clear()

    if timetable_rows:
        _flush_routes(conn, list(route_cache.values()))
        _flush_timetables(conn, timetable_rows)

    _flush_routes(conn, list(route_cache.values()))
    log.info("Rail ingestion complete: %d trips, %d routes.",
             trip_count, len(route_cache))


def _flush_routes(conn, route_rows: Iterable[Dict[str, str]]) -> None:
    rows = list(route_rows)
    if not rows:
        return
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO routes (route_id, route_name, operator, description, route_type, active)
            VALUES %s
            ON CONFLICT (route_id) DO UPDATE SET
                route_name = EXCLUDED.route_name,
                operator = EXCLUDED.operator,
                description = EXCLUDED.description,
                route_type = EXCLUDED.route_type,
                active = TRUE
            """,
            [
                (
                    r["route_id"],
                    r["route_name"],
                    r["operator"],
                    r["description"],
                    r["route_type"],
                    True,
                )
                for r in rows
            ],
        )
    conn.commit()


def _flush_timetables(conn, rows: List[Tuple]) -> None:
    with conn.cursor() as cur:
        execute_values(
            cur,
            """
            INSERT INTO timetables (
                route_id, stop_id, trip_id,
                arrival_time, departure_time,
                stop_sequence, direction,
                days_of_week, valid_from, valid_until
            )
            VALUES %s
            """,
            rows,
        )
    conn.commit()


def run_ingestion() -> None:
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        tiploc_to_crs = load_corpus_tiploc_map()
        crs_stops = load_crs_stops_from_db(conn)
        if not crs_stops:
            crs_stops = load_crs_stops_from_naptan(conn)

        if not crs_stops:
            raise RuntimeError(
                "No CRS stop mappings available; ingest_stops may need to be run.")

        clear_existing_rail_data(conn)
        ingest_schedule(conn, tiploc_to_crs, crs_stops)
    finally:
        conn.close()


if __name__ == "__main__":
    run_ingestion()
