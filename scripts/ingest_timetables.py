import requests
import psycopg2
import zipfile
import io
from lxml import etree
import urllib3
from datetime import datetime, timedelta
import re

# Silence SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DB_CONFIG = {
    "host": "localhost",
    "port": "5432",
    "user": "transport",
    "password": "transport_dev",
    "dbname": "transport_db"
}

# All operators from the project
OPERATORS = ["ARCT", "BLAC", "KLCO", "SCCU", "SCMY", "NUTT"]

# TransXChange namespace
NS = {'txc': 'http://www.transxchange.org.uk/'}


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


def parse_txc_xml(conn, xml_content, operator_code):
    """Parse TransXChange XML and insert into routes, route_stops, timetables, and route_waypoints."""
    try:
        root = etree.fromstring(xml_content)
    except Exception as e:
        print(f"      XML parse error: {e}")
        return

    # 1. Parse JourneyPatternSections — maps section_id -> list of (stop_ref, sequence, run_time_secs)
    sections = {}
    for section in root.findall('.//txc:JourneyPatternSection', namespaces=NS):
        section_id = section.get('id')
        stops = []
        for link in section.findall('txc:JourneyPatternTimingLink', namespaces=NS):
            from_elem = link.find('txc:From', namespaces=NS)
            to_elem = link.find('txc:To', namespaces=NS)
            run_time = parse_duration(
                link.findtext('txc:RunTime', namespaces=NS))

            if from_elem is not None and not stops:
                seq = int(from_elem.get('SequenceNumber', 0))
                stop_ref = from_elem.findtext(
                    'txc:StopPointRef', namespaces=NS)
                if stop_ref:
                    stops.append((stop_ref, seq, 0))

            if to_elem is not None:
                seq = int(to_elem.get('SequenceNumber', 0))
                stop_ref = to_elem.findtext('txc:StopPointRef', namespaces=NS)
                if stop_ref:
                    stops.append((stop_ref, seq, run_time))

        sections[section_id] = stops

    # 2. Parse JourneyPatterns — maps pattern_id -> (section_id, direction)
    patterns = {}
    # Also capture optional RouteRef per journey pattern (used for geometry)
    pattern_route_refs = {}
    for service in root.findall('.//txc:Service', namespaces=NS):
        for jp in service.findall('.//txc:JourneyPattern', namespaces=NS):
            jp_id = jp.get('id')
            direction = jp.findtext(
                'txc:Direction', namespaces=NS) or 'outbound'
            section_ref = jp.findtext(
                'txc:JourneyPatternSectionRefs', namespaces=NS)
            route_ref = jp.findtext('txc:RouteRef', namespaces=NS)
            if jp_id and section_ref:
                patterns[jp_id] = (section_ref, direction)
            if jp_id and route_ref:
                pattern_route_refs[jp_id] = route_ref

    # 2b. Parse RouteSections — maps section_id -> list of (from_stop, to_stop, track_points)
    #     track_points is a list of (lat, lon) intermediate road waypoints.
    route_sections_geo = {}
    for rs in root.findall('.//txc:RouteSection', namespaces=NS):
        sid = rs.get('id')
        links = []
        for rl in rs.findall('txc:RouteLink', namespaces=NS):
            from_sp = rl.findtext('txc:From/txc:StopPointRef', namespaces=NS)
            to_sp = rl.findtext('txc:To/txc:StopPointRef', namespaces=NS)
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

    # 2c. Parse Routes — maps Route element id -> RouteSectionRef
    routes_to_section = {}
    for route in root.findall('.//txc:Route', namespaces=NS):
        rid = route.get('id')
        sref = route.findtext('txc:RouteSectionRef', namespaces=NS)
        if rid and sref:
            routes_to_section[rid] = sref

    # 3. Parse Service info
    service_elem = root.find('.//txc:Service', namespaces=NS)
    if service_elem is None:
        return

    service_code = service_elem.findtext('txc:ServiceCode', namespaces=NS)
    line_name = service_elem.findtext('.//txc:LineName', namespaces=NS)
    description = service_elem.findtext('txc:Description', namespaces=NS) or ''
    start_date = service_elem.findtext('.//txc:StartDate', namespaces=NS)
    end_date = service_elem.findtext('.//txc:EndDate', namespaces=NS)
    mode = service_elem.findtext('txc:Mode', namespaces=NS) or 'bus'

    if not service_code or not line_name:
        return

    # Use service_code as route_id
    route_id = service_code

    with conn.cursor() as cur:
        # Load valid stop IDs from our database
        cur.execute("SELECT stop_id FROM stops")
        valid_stops = {row[0] for row in cur.fetchall()}

        # 4. Insert into routes table
        cur.execute("""
            INSERT INTO routes (route_id, route_name, operator, description, route_type)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (route_id) DO UPDATE SET
                route_name = EXCLUDED.route_name,
                operator = EXCLUDED.operator,
                description = EXCLUDED.description;
        """, (route_id, line_name, operator_code, description, mode))

        # 5. Insert into route_stops for each journey pattern
        route_stops_inserted = set()
        for jp_id, (section_id, direction) in patterns.items():
            if section_id not in sections:
                continue
            for stop_ref, seq, _ in sections[section_id]:
                if stop_ref not in valid_stops:
                    continue
                key = (route_id, stop_ref, direction, seq)
                if key not in route_stops_inserted:
                    cur.execute("""
                        INSERT INTO route_stops (route_id, stop_id, stop_sequence, direction)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (route_id, stop_id, direction, stop_sequence) DO NOTHING;
                    """, (route_id, stop_ref, seq, direction))
                    route_stops_inserted.add(key)

        # 5b. Insert route_waypoints for each (route_id, direction).
        # Only insert once per direction; skip if already done by a previous JP.
        cur.execute(
            "SELECT stop_id, latitude, longitude FROM stops WHERE stop_id = ANY(%s)",
            (list(valid_stops),)
        )
        stop_coords = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

        inserted_wp_directions = set()
        for jp_id, (section_id, direction) in patterns.items():
            wp_key = (route_id, direction)
            if wp_key in inserted_wp_directions:
                continue

            waypoints = []

            # Prefer track geometry from RouteSections / Routes if available.
            route_ref = pattern_route_refs.get(jp_id)
            geo_section_id = routes_to_section.get(route_ref) if route_ref else None
            if geo_section_id and geo_section_id in route_sections_geo:
                waypoints = _build_waypoints_from_links(
                    route_sections_geo[geo_section_id], stop_coords
                )

            # Fallback: use ordered stop coordinates from the journey pattern.
            if not waypoints and section_id in sections:
                waypoints = _build_waypoints_from_stops(
                    sections[section_id], stop_coords
                )

            for seq_num, (lat, lon, stop_id) in enumerate(waypoints):
                cur.execute("""
                    INSERT INTO route_waypoints
                        (route_id, direction, sequence, latitude, longitude, stop_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (route_id, direction, sequence) DO NOTHING;
                """, (route_id, direction, seq_num, lat, lon, stop_id))

            if waypoints:
                inserted_wp_directions.add(wp_key)

        # Default placeholder run time (seconds) used when a timing link
        # carries no duration (PT0M0S / 0s).  1 minute per link gives each
        # stop a distinct, incrementing time so that the journey planner can
        # distinguish origin departure from destination arrival.
        PLACEHOLDER_RUN_TIME_SECS = 60

        # 6. Parse VehicleJourneys and insert into timetables
        journey_count = 0
        for vj in root.findall('.//txc:VehicleJourney', namespaces=NS):
            departure_str = vj.findtext('txc:DepartureTime', namespaces=NS)
            jp_ref = vj.findtext('txc:JourneyPatternRef', namespaces=NS)
            vj_code = vj.findtext(
                'txc:VehicleJourneyCode', namespaces=NS) or ''

            if not departure_str or not jp_ref or jp_ref not in patterns:
                continue

            section_id, direction = patterns[jp_ref]
            if section_id not in sections:
                continue

            # Parse days of week
            days_elem = vj.find('.//txc:DaysOfWeek', namespaces=NS)
            days_bitmask = days_to_bitmask(days_elem)

            # Calculate arrival/departure times by accumulating run times
            dep_parts = departure_str.split(':')
            base_hour, base_min, base_sec = int(dep_parts[0]), int(
                dep_parts[1]), int(dep_parts[2]) if len(dep_parts) > 2 else 0
            cumulative_secs = base_hour * 3600 + base_min * 60 + base_sec

            for i, (stop_ref, seq, run_time) in enumerate(sections[section_id]):
                # The first stop legitimately has zero travel time (it is the
                # origin).  For every subsequent stop, substitute the placeholder
                # when the source data provides no run time.
                effective_run_time = run_time if (i == 0 or run_time > 0) else PLACEHOLDER_RUN_TIME_SECS
                # Accumulate the travel time to reach this stop first,
                # so arrival_secs reflects when the bus arrives here.
                cumulative_secs += effective_run_time
                arrival_secs = cumulative_secs
                departure_secs = cumulative_secs

                # Skip stops not in our database
                if stop_ref not in valid_stops:
                    continue

                # Convert seconds back to time string
                arr_h = (arrival_secs // 3600) % 24
                arr_m = (arrival_secs % 3600) // 60
                arr_s = arrival_secs % 60
                arrival_time = f"{arr_h:02d}:{arr_m:02d}:{arr_s:02d}"
                departure_time = arrival_time

                cur.execute("""
                    INSERT INTO timetables (route_id, stop_id, trip_id, arrival_time, departure_time, stop_sequence, direction, days_of_week, valid_from, valid_until)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT DO NOTHING;
                """, (route_id, stop_ref, vj_code, arrival_time, departure_time, seq, direction, days_bitmask, start_date, end_date))

                journey_count += 1

        if journey_count > 0:
            print(
                f"      Route {line_name} ({route_id}): {journey_count} timetable entries")

    conn.commit()


def download_and_extract(conn, zip_url, operator_code):
    """Download ZIP and process contained XML files."""
    print(f"   Downloading ZIP from: {zip_url}")
    try:
        response = requests.get(zip_url, verify=False, timeout=30)
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            # Process every XML file inside the ZIP
            xml_files = [f for f in z.namelist() if f.endswith('.xml')]
            print(f"    Found {len(xml_files)} XML files")
            for filename in xml_files:
                parse_txc_xml(conn, z.read(filename), operator_code)
    except Exception as e:
        print(f"   Error processing ZIP: {e}")


def run_ingestion():
    """Main execution logic — fetches all operators from the Discovery API."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        # No table creation — schema managed by init_db.sql

        for operator in OPERATORS:
            discovery_url = f"https://transport.scc.lancs.ac.uk/bus/times/{operator}"
            print(
                f"\n Fetching discovery data for {operator} from {discovery_url}...")

            try:
                response = requests.get(
                    discovery_url, verify=False, timeout=15)
                data = response.json()
            except Exception as e:
                print(f"   Error fetching {operator}: {e}")
                continue

            # Access the 'results' list from the API
            results = data.get('results', [])
            print(f"  Found {len(results)} dataset records for {operator}.")

            for item in results:
                # Get the download URL and extension from the JSON
                zip_url = item.get('url')
                ext = item.get('extension')

                if zip_url and ext == "zip":
                    download_and_extract(conn, zip_url, operator)

        conn.close()
        print("\n All timetable data processed successfully!")

    except Exception as e:
        print(f" Ingestion failed: {e}")


if __name__ == "__main__":
    run_ingestion()
