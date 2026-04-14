import logging
import os
import requests
import psycopg2
from psycopg2.extras import execute_values
import zipfile
import io
from lxml import etree
import urllib3
from datetime import datetime, timedelta
import re
from collections import defaultdict

# Silence SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": os.environ.get("DB_PORT", "5432"),
    "user": os.environ.get("DB_USER", "transport"),
    "password": os.environ.get("DB_PASSWORD", "transport_dev"),
    "dbname": os.environ.get("DB_NAME", "transport_db")
}

# All operators from the project
OPERATORS = ["ARCT", "BLAC", "KLCO", "SCCU", "SCMY", "NUTT"]

# Preferred Stagecoach regional datasets used to avoid importing all
# nationwide Stagecoach timetable bundles for SCCU/SCMY ingestion.
TARGET_STAGECOACH_DESCRIPTIONS = {
    "Stagecoach Cumbria & North Lancashire",
    "Stagecoach Merseyside & South Lancashire",
}

# TransXChange namespace
NS = {'txc': 'http://www.transxchange.org.uk/'}

# Approximate bounding box for North West Lancashire / Cumbria.
# Extended westward to -3.7 to cover the full SCCU service area.
_LAT_MIN, _LAT_MAX = 53.0, 55.0
_LON_MIN, _LON_MAX = -3.7, -2.0


def _sanitize_stop_ref(stop_ref):
    """Strip whitespace from a stop reference string, return None if empty."""
    if stop_ref is None:
        return None
    cleaned = str(stop_ref).strip()
    return cleaned if cleaned else None


def _validate_stop_coords(stop_coords):
    """
    Return a filtered copy of stop_coords that contains only entries whose
    latitude/longitude values fall within the NW Lancashire bounding box.
    Entries with non-numeric or out-of-range values are dropped with a warning.
    """
    valid = {}
    for stop_id, (lat, lon) in stop_coords.items():
        try:
            lat_f = float(lat)
            lon_f = float(lon)
        except (TypeError, ValueError):
            log.warning(
                "Stop %s has non-numeric coordinates — excluded from geometry.", stop_id)
            continue
        if not (_LAT_MIN <= lat_f <= _LAT_MAX) or not (_LON_MIN <= lon_f <= _LON_MAX):
            log.debug(
                "Stop %s coordinates (%.5f, %.5f) outside NW Lancashire bounds — excluded.",
                stop_id, lat_f, lon_f,
            )
            continue
        valid[stop_id] = (lat_f, lon_f)
    return valid


def parse_duration(duration_str):
    """Parse ISO 8601 duration like PT1M7S into total seconds."""
    if not duration_str:
        return 0
    match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
    if not match:
        return 0
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2) or 0)
    seconds = int(match.group(3) or 0)
    return hours * 3600 + minutes * 60 + seconds


def _activity_flags(activity_value):
    """Return (pickup_allowed, setdown_allowed) for a TXC Activity value."""
    if not activity_value:
        return True, True

    key = str(activity_value).strip().lower()
    if key == 'pickupandsetdown':
        return True, True
    if key == 'pickup':
        return True, False
    if key == 'setdown':
        return False, True

    # e.g. pass
    return False, False


def days_to_bitmask(days_elem):
    """Convert DaysOfWeek element to bitmask. Mon=1, Tue=2, Wed=4, Thu=8, Fri=16, Sat=32, Sun=64."""
    if days_elem is None:
        return 127  # Default: all days

    day_map = {
        'Monday': 1, 'Tuesday': 2, 'Wednesday': 4, 'Thursday': 8,
        'Friday': 16, 'Saturday': 32, 'Sunday': 64,
        'MondayToFriday': 31, 'MondayToSaturday': 63, 'MondayToSunday': 127,
    }

    bitmask = 0
    for child in days_elem:
        tag = child.tag.split('}')[-1]  # Remove namespace
        if tag in day_map:
            bitmask |= day_map[tag]

    return bitmask if bitmask > 0 else 127


def _build_waypoints_from_links(links, stop_coords):
    """
    Build an ordered list of (lat, lon, stop_id) waypoints from RouteLink geometry data.

    For each RouteLink the first point is the *from* stop and the last point is the
    *to* stop (both taken from the stops table so the coordinates are accurate).
    Any intermediate Track points that exist between those two stops are inserted in
    between, giving a road-following polyline.  Consecutive RouteLinks share a stop
    (the *to* of one link equals the *from* of the next), so the shared stop is only
    appended once.
    """
    waypoints = []
    for i, (from_sp, to_sp, track_pts) in enumerate(links):
        # Add from_stop for the very first link only (subsequent links already added
        # this stop as the *to* of the previous link).
        if i == 0 and from_sp in stop_coords:
            lat, lon = stop_coords[from_sp]
            waypoints.append((lat, lon, from_sp))

        # Add intermediate track points, skipping the first and last positions in
        # the raw track data because they are (approximately) the stop locations
        # which we handle explicitly with verified DB coordinates.
        if len(track_pts) > 2:
            for lat, lon in track_pts[1:-1]:
                waypoints.append((lat, lon, None))

        # Add to_stop.
        if to_sp in stop_coords:
            lat, lon = stop_coords[to_sp]
            waypoints.append((lat, lon, to_sp))

    return waypoints


def _build_waypoints_from_stops(stop_sequence, stop_coords):
    """
    Fallback: build waypoints from the ordered stop sequence when no road-following
    track geometry is available.  Each waypoint is marked with its stop_id.
    """
    waypoints = []
    for stop_ref, _seq, _run_time in stop_sequence:
        if stop_ref in stop_coords:
            lat, lon = stop_coords[stop_ref]
            waypoints.append((lat, lon, stop_ref))
    return waypoints


def _build_route_id(service_code, line_ref, line_name):
    """Build a stable per-line route_id from service code and line identity."""
    line_suffix = None
    if line_ref:
        line_suffix = line_ref.strip().split(':')[-1]
    if not line_suffix and line_name:
        line_suffix = str(line_name).strip()
    if line_suffix:
        return f"{service_code}:{line_suffix}"
    return service_code


def _ensure_route_waypoint_variant_schema(conn):
    """Ensure route_waypoints supports geometry variants within a direction."""
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE route_waypoints "
            "ADD COLUMN IF NOT EXISTS variant_id VARCHAR(100)"
        )
        cur.execute(
            "UPDATE route_waypoints "
            "SET variant_id = 'default' "
            "WHERE variant_id IS NULL OR variant_id = ''"
        )
        cur.execute(
            "ALTER TABLE route_waypoints "
            "ALTER COLUMN variant_id SET DEFAULT 'default'"
        )
        cur.execute(
            "ALTER TABLE route_waypoints "
            "ALTER COLUMN variant_id SET NOT NULL"
        )

        # Drop legacy uniqueness by (route_id, direction, sequence)
        # so multiple variants can coexist.
        cur.execute(
            "ALTER TABLE route_waypoints "
            "DROP CONSTRAINT IF EXISTS route_waypoints_route_id_direction_sequence_key"
        )

        cur.execute("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conname = 'uq_route_waypoints_variant_seq'
                ) THEN
                    ALTER TABLE route_waypoints
                    ADD CONSTRAINT uq_route_waypoints_variant_seq
                    UNIQUE (route_id, direction, variant_id, sequence);
                END IF;
            END$$;
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_route_waypoints_variant "
            "ON route_waypoints(route_id, direction, variant_id)"
        )
    conn.commit()


def _ensure_timetables_call_activity_schema(conn):
    """Ensure timetables has explicit pickup/setdown flags per stop call."""
    with conn.cursor() as cur:
        cur.execute(
            "ALTER TABLE timetables "
            "ADD COLUMN IF NOT EXISTS pickup_allowed BOOLEAN"
        )
        cur.execute(
            "ALTER TABLE timetables "
            "ADD COLUMN IF NOT EXISTS setdown_allowed BOOLEAN"
        )
        cur.execute(
            "UPDATE timetables "
            "SET pickup_allowed = COALESCE(pickup_allowed, TRUE), "
            "    setdown_allowed = COALESCE(setdown_allowed, TRUE)"
        )
        cur.execute(
            "ALTER TABLE timetables "
            "ALTER COLUMN pickup_allowed SET DEFAULT TRUE"
        )
        cur.execute(
            "ALTER TABLE timetables "
            "ALTER COLUMN setdown_allowed SET DEFAULT TRUE"
        )
        cur.execute(
            "ALTER TABLE timetables "
            "ALTER COLUMN pickup_allowed SET NOT NULL"
        )
        cur.execute(
            "ALTER TABLE timetables "
            "ALTER COLUMN setdown_allowed SET NOT NULL"
        )
    conn.commit()


def parse_txc_xml(conn, xml_content, operator_code, valid_stops, stop_coords, processed_route_ids=None):
    """Parse TransXChange XML and insert into routes, route_stops, timetables, and route_waypoints.

    Args:
        conn: psycopg2 connection (must already be open).
        xml_content: raw bytes of the TransXChange XML file.
        operator_code: fallback operator code (e.g. "ARCT") used only when
            the XML does not contain a <NationalOperatorCode> element.
        valid_stops: set of stop_id strings known to be in the stops table.
        stop_coords: dict mapping stop_id -> (latitude, longitude).
                     Values have already been validated by _validate_stop_coords().
    """
    try:
        root = etree.fromstring(xml_content)
    except Exception as e:
        log.warning("XML parse error: %s", e)
        return

    # --- Determine the real operator from the XML itself ---
    # TransXChange files embed one or more <Operator> elements, each with a
    # <NationalOperatorCode>.  Use the first one found; fall back to the
    # caller-supplied operator_code only when the element is absent.
    xml_noc = None
    for op_elem in root.findall('.//txc:Operator', namespaces=NS):
        noc = (op_elem.findtext('txc:NationalOperatorCode',
               namespaces=NS) or '').strip()
        if noc:
            xml_noc = noc
            break
    if xml_noc and xml_noc != operator_code:
        log.debug(
            "XML NationalOperatorCode '%s' overrides caller-supplied '%s'.",
            xml_noc, operator_code,
        )
        operator_code = xml_noc

    # 1. Parse JourneyPatternSections.
    #    - sections maps section_id -> list of (stop_ref, sequence, run_time_secs)
    #      (kept for route_stops fallback compatibility)
    #    - section_timing_links maps section_id -> ordered list of timing links,
    #      each carrying from/to stop, runtime and from-stop wait time.
    sections = {}
    section_timing_links = {}
    for section in root.findall('.//txc:JourneyPatternSection', namespaces=NS):
        section_id = section.get('id')
        stops = []
        timing_links = []
        for link in section.findall('txc:JourneyPatternTimingLink', namespaces=NS):
            jptl_id = (link.get('id') or '').strip()
            from_elem = link.find('txc:From', namespaces=NS)
            to_elem = link.find('txc:To', namespaces=NS)
            run_time = parse_duration(
                link.findtext('txc:RunTime', namespaces=NS))

            from_activity = (from_elem.findtext('txc:Activity', namespaces=NS)
                             if from_elem is not None else None)
            to_activity = (to_elem.findtext('txc:Activity', namespaces=NS)
                           if to_elem is not None else None)

            from_wait_time = 0
            if from_elem is not None:
                from_wait_time = parse_duration(
                    from_elem.findtext('txc:WaitTime', namespaces=NS)
                )

            if from_elem is not None and not stops:
                seq = int(from_elem.get('SequenceNumber', 0))
                stop_ref = _sanitize_stop_ref(
                    from_elem.findtext('txc:StopPointRef', namespaces=NS))
                if stop_ref:
                    stops.append((stop_ref, seq, 0))

            if to_elem is not None:
                seq = int(to_elem.get('SequenceNumber', 0))
                stop_ref = _sanitize_stop_ref(
                    to_elem.findtext('txc:StopPointRef', namespaces=NS))
                if stop_ref:
                    stops.append((stop_ref, seq, run_time))

            from_ref = _sanitize_stop_ref(
                from_elem.findtext('txc:StopPointRef', namespaces=NS)
            ) if from_elem is not None else None
            to_ref = _sanitize_stop_ref(
                to_elem.findtext('txc:StopPointRef', namespaces=NS)
            ) if to_elem is not None else None
            from_seq = int(from_elem.get('SequenceNumber', 0)
                           ) if from_elem is not None else 0
            to_seq = int(to_elem.get('SequenceNumber', 0)
                         ) if to_elem is not None else 0

            if from_ref and to_ref:
                timing_links.append({
                    'jptl_id': jptl_id,
                    'from_stop': from_ref,
                    'from_seq': from_seq,
                    'to_stop': to_ref,
                    'to_seq': to_seq,
                    'run_time': run_time,
                    'from_wait_time': from_wait_time,
                    'from_activity': from_activity,
                    'to_activity': to_activity,
                })

        sections[section_id] = stops
        section_timing_links[section_id] = timing_links

    # 2. Parse JourneyPatterns — maps pattern_id -> (list_of_section_ids, direction)
    # Also capture optional RouteRef per journey pattern (used for geometry)
    patterns = {}
    pattern_route_refs = {}
    for service in root.findall('.//txc:Service', namespaces=NS):
        for jp in service.findall('.//txc:JourneyPattern', namespaces=NS):
            jp_id = jp.get('id')
            direction = jp.findtext(
                'txc:Direction', namespaces=NS) or 'outbound'
            # A JourneyPattern may reference multiple JourneyPatternSectionRefs;
            # collect them in order rather than taking only the first.
            section_refs = [
                (elem.text or '').strip()
                for elem in jp.findall('txc:JourneyPatternSectionRefs', namespaces=NS)
                if (elem.text or '').strip()
            ]
            route_ref = jp.findtext('txc:RouteRef', namespaces=NS)
            if jp_id and section_refs:
                patterns[jp_id] = (section_refs, direction)
            if jp_id and route_ref:
                pattern_route_refs[jp_id] = route_ref

    # 2b. Parse RouteSections — maps section_id -> list of (from_stop, to_stop, track_points)
    #     track_points is a list of (lat, lon) intermediate road waypoints.
    route_sections_geo = {}
    for rs in root.findall('.//txc:RouteSection', namespaces=NS):
        sid = rs.get('id')
        links = []
        for rl in rs.findall('txc:RouteLink', namespaces=NS):
            from_sp = _sanitize_stop_ref(rl.findtext(
                'txc:From/txc:StopPointRef', namespaces=NS))
            to_sp = _sanitize_stop_ref(rl.findtext(
                'txc:To/txc:StopPointRef', namespaces=NS))
            track_pts = []
            for loc in rl.findall('.//txc:Location', namespaces=NS):
                lat_str = loc.findtext('txc:Latitude', namespaces=NS)
                lon_str = loc.findtext('txc:Longitude', namespaces=NS)
                if lat_str and lon_str:
                    try:
                        track_pts.append((float(lat_str), float(lon_str)))
                    except ValueError:
                        pass
            links.append((from_sp, to_sp, track_pts))
        if links:
            route_sections_geo[sid] = links

    # 2c. Parse Routes — maps Route element id -> list of RouteSectionRef ids
    routes_to_section = {}
    for route in root.findall('.//txc:Route', namespaces=NS):
        rid = route.get('id')
        # A Route may reference multiple RouteSectionRefs; collect them in order.
        srefs = [
            (elem.text or '').strip()
            for elem in route.findall('txc:RouteSectionRef', namespaces=NS)
            if (elem.text or '').strip()
        ]
        if rid and srefs:
            routes_to_section[rid] = srefs

    # 3. Parse Service info
    service_elem = root.find('.//txc:Service', namespaces=NS)
    if service_elem is None:
        return

    service_code = service_elem.findtext('txc:ServiceCode', namespaces=NS)
    description = service_elem.findtext('txc:Description', namespaces=NS) or ''
    start_date = service_elem.findtext('.//txc:StartDate', namespaces=NS)
    end_date = service_elem.findtext('.//txc:EndDate', namespaces=NS)
    mode = service_elem.findtext('txc:Mode', namespaces=NS) or 'bus'

    if not service_code:
        return

    # Map full LineRef IDs to a user-visible line name (e.g. "1A").
    line_name_by_ref = {}
    for line_elem in service_elem.findall('txc:Lines/txc:Line', namespaces=NS):
        line_ref = (line_elem.get('id') or '').strip()
        line_name = (line_elem.findtext(
            'txc:LineName', namespaces=NS) or '').strip()
        if line_ref and line_name:
            line_name_by_ref[line_ref] = line_name

    default_line_name = service_elem.findtext(
        './/txc:LineName', namespaces=NS) or service_code

    # Build per-line route metadata and timetable rows from vehicle journeys.
    route_names = {}
    route_pattern_refs = defaultdict(set)
    timetables_batch = []
    journey_counts = defaultdict(int)

    for vj in root.findall('.//txc:VehicleJourney', namespaces=NS):
        departure_str = vj.findtext('txc:DepartureTime', namespaces=NS)
        jp_ref = vj.findtext('txc:JourneyPatternRef', namespaces=NS)
        vj_code = vj.findtext('txc:VehicleJourneyCode', namespaces=NS) or ''
        line_ref = (vj.findtext('txc:LineRef', namespaces=NS) or '').strip()
        line_name = line_name_by_ref.get(line_ref) or default_line_name

        if not departure_str or not jp_ref or jp_ref not in patterns:
            continue

        route_id = _build_route_id(service_code, line_ref, line_name)
        route_names[route_id] = line_name
        route_pattern_refs[route_id].add(jp_ref)

        section_ids, direction = patterns[jp_ref]

        # VehicleJourney-level overrides (runtime/wait) for JourneyPattern links.
        vj_link_overrides = {}
        for vj_link in vj.findall('txc:VehicleJourneyTimingLink', namespaces=NS):
            jptl_ref = (vj_link.findtext(
                'txc:JourneyPatternTimingLinkRef', namespaces=NS) or '').strip()
            if not jptl_ref:
                continue
            ov_run_time = parse_duration(
                vj_link.findtext('txc:RunTime', namespaces=NS)
            )
            ov_wait_time = parse_duration(
                vj_link.findtext('txc:From/txc:WaitTime', namespaces=NS)
            )
            ov_from_activity = vj_link.findtext(
                'txc:From/txc:Activity', namespaces=NS)
            ov_to_activity = vj_link.findtext(
                'txc:To/txc:Activity', namespaces=NS)
            vj_link_overrides[jptl_ref] = {
                'run_time': ov_run_time,
                'from_wait_time': ov_wait_time,
                'from_activity': ov_from_activity,
                'to_activity': ov_to_activity,
            }

        # Build combined timing links from all referenced journey pattern sections.
        combined_links = []
        for section_id in (section_ids if isinstance(section_ids, (list, tuple)) else [section_ids]):
            if section_id in section_timing_links:
                combined_links.extend(section_timing_links[section_id])
        if not combined_links:
            continue

        # Apply per-vehicle-journey timing overrides where present.
        if vj_link_overrides:
            merged_links = []
            for link in combined_links:
                override = vj_link_overrides.get(link['jptl_id'])
                if override:
                    merged_links.append({
                        **link,
                        'run_time': override.get('run_time', link['run_time']),
                        'from_wait_time': override.get('from_wait_time', link['from_wait_time']),
                        'from_activity': override.get('from_activity') or link.get('from_activity'),
                        'to_activity': override.get('to_activity') or link.get('to_activity'),
                    })
                else:
                    merged_links.append(link)
            combined_links = merged_links

        days_elem = vj.find('.//txc:DaysOfWeek', namespaces=NS)
        days_bitmask = days_to_bitmask(days_elem)

        # Build a globally unique trip_id per logical route AND operating
        # period.  The same VehicleJourneyCode (e.g. VJ337) is reused across
        # multiple XML files that cover different date ranges.  Without the
        # period tag the unique constraint (route_id, trip_id, stop_sequence)
        # causes ON CONFLICT DO NOTHING to silently merge or drop stops from
        # later files, producing corrupted Frankenstein trips that mix stop
        # sequences from unrelated journeys.
        period_tag = (start_date or "").replace("-", "")
        trip_id = (
            f"{route_id}_{vj_code}_{period_tag}"
            if vj_code
            else f"{route_id}_{departure_str}_{direction}_{period_tag}"
        )

        # Calculate arrival/departure times from link runtime + wait (loitering)
        # values. WaitTime is applied at the FROM stop before traversing the link.
        dep_parts = departure_str.split(':')
        base_hour, base_min, base_sec = int(dep_parts[0]), int(
            dep_parts[1]), int(dep_parts[2]) if len(dep_parts) > 2 else 0
        base_secs = base_hour * 3600 + base_min * 60 + base_sec

        def _secs_to_hms(total_secs: int) -> str:
            h = (total_secs // 3600) % 24
            m = (total_secs % 3600) // 60
            s = total_secs % 60
            return f"{h:02d}:{m:02d}:{s:02d}"

        first_link = combined_links[0]
        first_pickup, first_setdown = _activity_flags(
            first_link.get('from_activity'))
        # Use a monotonically increasing sequence counter per trip
        # instead of the XML SequenceNumber which is scoped per-section
        # and overlaps when multiple sections are concatenated.
        trip_seq_counter = 1
        stop_records = [{
            'stop_ref': first_link['from_stop'],
            'seq': trip_seq_counter,
            'arrival_secs': base_secs,
            'departure_secs': base_secs,
            'pickup_allowed': first_pickup,
            'setdown_allowed': first_setdown,
        }]

        for idx, link in enumerate(combined_links):
            current = stop_records[-1]

            # If section boundaries produce a mismatch, realign to the link FROM stop.
            if current['stop_ref'] != link['from_stop']:
                from_pickup, from_setdown = _activity_flags(
                    link.get('from_activity'))
                trip_seq_counter += 1
                stop_records.append({
                    'stop_ref': link['from_stop'],
                    'seq': trip_seq_counter,
                    'arrival_secs': current['departure_secs'],
                    'departure_secs': current['departure_secs'],
                    'pickup_allowed': from_pickup,
                    'setdown_allowed': from_setdown,
                })
                current = stop_records[-1]

            # Apply dwell/loiter wait time at FROM stop (not on the first origin stop).
            wait_secs = int(link.get('from_wait_time') or 0)
            if idx > 0 and wait_secs > 0:
                current['departure_secs'] = max(
                    current['departure_secs'],
                    current['arrival_secs'] + wait_secs
                )

            run_secs = int(link.get('run_time') or 0)
            arrival_to_secs = current['departure_secs'] + run_secs

            to_pickup, to_setdown = _activity_flags(link.get('to_activity'))

            trip_seq_counter += 1
            stop_records.append({
                'stop_ref': link['to_stop'],
                'seq': trip_seq_counter,
                'arrival_secs': arrival_to_secs,
                'departure_secs': arrival_to_secs,
                'pickup_allowed': to_pickup,
                'setdown_allowed': to_setdown,
            })

        for rec in stop_records:
            stop_ref = rec['stop_ref']
            if stop_ref not in valid_stops:
                continue

            arrival_time = _secs_to_hms(rec['arrival_secs'])
            departure_time = _secs_to_hms(rec['departure_secs'])

            timetables_batch.append((
                route_id, stop_ref, trip_id,
                arrival_time, departure_time,
                rec['seq'], direction, days_bitmask, start_date, end_date,
                rec['pickup_allowed'], rec['setdown_allowed'],
            ))
            journey_counts[route_id] += 1

    if not route_names:
        return

    if processed_route_ids is None:
        processed_route_ids = set()

    with conn.cursor() as cur:
        # 4. Insert/update one route row per line (e.g. 1 and 1A).
        routes_batch = [
            (rid, rname, operator_code, description, mode)
            for rid, rname in route_names.items()
        ]
        execute_values(cur, """
            INSERT INTO routes (route_id, route_name, operator, description, route_type)
            VALUES %s
            ON CONFLICT (route_id) DO UPDATE SET
                route_name = EXCLUDED.route_name,
                operator = EXCLUDED.operator,
                description = EXCLUDED.description
        """, routes_batch)

        # Clear existing rows only once per route during this ingestion run.
        # This avoids wiping out routes when multiple XML files for the same
        # service are processed sequentially (where each file may contain only
        # a subset of journeys).
        first_seen_route_ids = [
            rid for rid in route_names.keys() if rid not in processed_route_ids
        ]
        if first_seen_route_ids:
            cur.execute(
                "DELETE FROM timetables WHERE route_id = ANY(%s)",
                (first_seen_route_ids,),
            )
            cur.execute(
                "DELETE FROM route_stops WHERE route_id = ANY(%s)",
                (first_seen_route_ids,),
            )
            cur.execute(
                "DELETE FROM route_waypoints WHERE route_id = ANY(%s)",
                (first_seen_route_ids,),
            )
            processed_route_ids.update(first_seen_route_ids)

        # 5. Collect route_stops rows, then batch-insert.
        route_stops_seen = set()
        route_stops_batch = []
        for route_id, jp_refs in route_pattern_refs.items():
            for jp_id in jp_refs:
                section_ids, direction = patterns[jp_id]
                for section_id in (section_ids if isinstance(section_ids, (list, tuple)) else [section_ids]):
                    if section_id not in sections:
                        continue
                    for stop_ref, seq, _ in sections[section_id]:
                        if stop_ref not in valid_stops:
                            continue
                        key = (route_id, stop_ref, direction, seq)
                        if key not in route_stops_seen:
                            route_stops_batch.append(
                                (route_id, stop_ref, seq, direction))
                            route_stops_seen.add(key)

        if route_stops_batch:
            execute_values(cur, """
                INSERT INTO route_stops (route_id, stop_id, stop_sequence, direction)
                VALUES %s
                ON CONFLICT (route_id, stop_id, direction, stop_sequence) DO NOTHING
            """, route_stops_batch)

        # 5b. Collect route_waypoints rows, then batch-insert.
        # Store one geometry set per (route_id, direction, variant_id).
        inserted_wp_variants = set()
        waypoints_batch = []
        for route_id, jp_refs in route_pattern_refs.items():
            for jp_id in jp_refs:
                section_ids, direction = patterns[jp_id]
                variant_id = (pattern_route_refs.get(jp_id)
                              or jp_id or "default").strip()
                wp_key = (route_id, direction, variant_id)
                if wp_key in inserted_wp_variants:
                    continue

                waypoints = []

                # Prefer track geometry from RouteSections / Routes if available.
                route_ref = pattern_route_refs.get(jp_id)
                geo_section_ids = routes_to_section.get(
                    route_ref) if route_ref else None
                # If multiple route sections are referenced, concatenate their links.
                if geo_section_ids:
                    combined_links = []
                    for gs in (geo_section_ids if isinstance(geo_section_ids, (list, tuple)) else [geo_section_ids]):
                        links = route_sections_geo.get(gs)
                        if links:
                            combined_links.extend(links)
                    if combined_links:
                        waypoints = _build_waypoints_from_links(
                            combined_links, stop_coords)

                # Fallback: use ordered stop coordinates from the journey pattern.
                if not waypoints:
                    combined_stop_seq = []
                    for section_id in (section_ids if isinstance(section_ids, (list, tuple)) else [section_ids]):
                        if section_id in sections:
                            combined_stop_seq.extend(sections[section_id])
                    if combined_stop_seq:
                        waypoints = _build_waypoints_from_stops(
                            combined_stop_seq, stop_coords)

                for seq_num, (lat, lon, stop_id) in enumerate(waypoints):
                    waypoints_batch.append(
                        (route_id, direction, variant_id, seq_num, lat, lon, stop_id))

                if waypoints:
                    inserted_wp_variants.add(wp_key)

        if waypoints_batch:
            execute_values(cur, """
                INSERT INTO route_waypoints
                    (route_id, direction, variant_id, sequence, latitude, longitude, stop_id)
                VALUES %s
                ON CONFLICT (route_id, direction, variant_id, sequence) DO NOTHING
            """, waypoints_batch)

        if timetables_batch:
            execute_values(cur, """
                INSERT INTO timetables
                    (route_id, stop_id, trip_id, arrival_time, departure_time,
                     stop_sequence, direction, days_of_week, valid_from, valid_until,
                     pickup_allowed, setdown_allowed)
                VALUES %s
                ON CONFLICT DO NOTHING
            """, timetables_batch)

        for route_id, journey_count in journey_counts.items():
            if journey_count > 0:
                log.info(
                    "Route %s (%s): %d timetable entries",
                    route_names.get(
                        route_id, route_id), route_id, journey_count,
                )

    conn.commit()


def download_and_extract(conn, zip_url, fallback_operator, valid_stops, stop_coords, processed_route_ids=None):
    """Download a ZIP or XML file and process any TransXChange XML content.

    The real operator code is extracted from each XML file's
    <NationalOperatorCode> element.  *fallback_operator* is only used when
    that element is missing.
    """
    log.info("Downloading from: %s", zip_url)
    try:
        response = requests.get(zip_url, verify=False, timeout=30)
        content = response.content
        # Attempt to process as a ZIP archive first; fall back to raw XML.
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as z:
                xml_files = [f for f in z.namelist(
                ) if f.lower().endswith('.xml')]
                log.info("Found %d XML files in ZIP", len(xml_files))
                for filename in xml_files:
                    parse_txc_xml(conn, z.read(filename),
                                  fallback_operator, valid_stops, stop_coords,
                                  processed_route_ids=processed_route_ids)
        except zipfile.BadZipFile:
            # Not a ZIP — treat the downloaded content as raw TransXChange XML.
            log.info("Response is not a ZIP; attempting to parse as raw XML.")
            parse_txc_xml(conn, content, fallback_operator,
                          valid_stops, stop_coords,
                          processed_route_ids=processed_route_ids)
    except Exception as e:
        log.error("Error processing %s: %s", zip_url, e)


def _select_discovery_results(operator, results):
    """Return filtered discovery rows for operators with targeted Stagecoach regions.

    For SCCU/SCMY we prefer only the two regional datasets by exact description.
    If one or both of those descriptions are missing, fall back to the full
    unfiltered operator dataset list.
    """
    if operator not in {"SCCU", "SCMY"}:
        return results

    matching = [
        item for item in results
        if (item.get("description") or "").strip() in TARGET_STAGECOACH_DESCRIPTIONS
    ]

    matched_descriptions = {
        (item.get("description") or "").strip()
        for item in matching
    }

    if matched_descriptions == TARGET_STAGECOACH_DESCRIPTIONS:
        log.info(
            "Using targeted Stagecoach datasets for %s (%d records).",
            operator,
            len(matching),
        )
        return matching

    missing = sorted(TARGET_STAGECOACH_DESCRIPTIONS - matched_descriptions)
    log.warning(
        "Target Stagecoach dataset descriptions not fully present for %s (missing: %s). "
        "Falling back to full operator dataset import.",
        operator,
        ", ".join(missing) if missing else "none",
    )
    return results


def run_ingestion():
    """Main execution logic — fetches all operators from the Discovery API."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        _ensure_route_waypoint_variant_schema(conn)
        _ensure_timetables_call_activity_schema(conn)
        # No table creation — schema managed by init_db.sql

        # Pre-load stop data once for all operators/files to avoid repeated DB queries.
        log.info("Pre-loading stop data from database...")
        with conn.cursor() as cur:
            cur.execute("SELECT stop_id FROM stops")
            valid_stops = {row[0] for row in cur.fetchall()}
            cur.execute("SELECT stop_id, latitude, longitude FROM stops")
            raw_coords = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

        # Validate coordinates so out-of-range stops do not corrupt geometry.
        stop_coords = _validate_stop_coords(raw_coords)
        excluded = len(raw_coords) - len(stop_coords)
        log.info(
            "Loaded %d stops (%d excluded for invalid/out-of-range coordinates).",
            len(stop_coords), excluded,
        )

        # Track dataset URLs already processed so the same ZIP is not
        # downloaded and parsed multiple times.  The discovery API often
        # returns identical datasets for related operator codes (e.g. all
        # Stagecoach subsidiaries share the same 13 dataset entries).
        seen_urls: set = set()
        # Track route_ids that have already been cleared in this run.
        processed_route_ids: set = set()

        for operator in OPERATORS:
            discovery_url = f"https://transport.scc.lancs.ac.uk/bus/times/{operator}"
            log.info("Fetching discovery data for %s from %s...",
                     operator, discovery_url)

            try:
                response = requests.get(
                    discovery_url, verify=False, timeout=15)
                data = response.json()
            except Exception as e:
                log.error("Error fetching %s: %s", operator, e)
                continue

            # Access the 'results' list from the API
            results = data.get('results', [])
            log.info("Found %d dataset records for %s.",
                     len(results), operator)

            selected_results = _select_discovery_results(operator, results)

            for item in selected_results:
                # Get the download URL and extension from the JSON
                zip_url = item.get('url')
                ext = item.get('extension')

                if zip_url and (ext or "").lower() in ("zip", "xml"):
                    if zip_url in seen_urls:
                        log.info("Skipping already-processed URL: %s", zip_url)
                        continue
                    seen_urls.add(zip_url)
                    # The operator from the outer loop is passed as a
                    # fallback only; parse_txc_xml extracts the real
                    # NationalOperatorCode from inside each XML file.
                    download_and_extract(
                        conn, zip_url, operator, valid_stops, stop_coords,
                        processed_route_ids=processed_route_ids)

        conn.close()
        log.info("All timetable data processed successfully.")

    except Exception as e:
        log.error("Ingestion failed: %s", e)


if __name__ == "__main__":
    run_ingestion()
