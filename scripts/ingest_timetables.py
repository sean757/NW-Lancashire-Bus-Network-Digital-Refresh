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


def parse_txc_xml(conn, xml_content, operator_code):
    """Parse TransXChange XML and insert into routes, route_stops, and timetables."""
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
    for service in root.findall('.//txc:Service', namespaces=NS):
        for jp in service.findall('.//txc:JourneyPattern', namespaces=NS):
            jp_id = jp.get('id')
            direction = jp.findtext(
                'txc:Direction', namespaces=NS) or 'outbound'
            section_ref = jp.findtext(
                'txc:JourneyPatternSectionRefs', namespaces=NS)
            if jp_id and section_ref:
                patterns[jp_id] = (section_ref, direction)

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

            # Skip journeys where all timing links have zero run time (e.g. PT0M0S),
            # which indicates missing or invalid timing data in the TXC XML file.
            # A valid multi-stop journey must have at least one non-zero run time.
            section_stops = sections[section_id]
            total_run_time = sum(run_time for _, _, run_time in section_stops)
            if total_run_time == 0 and len(section_stops) > 1:
                print(f"      Skipping journey {vj_code or jp_ref}: all timing links have zero run time (PT0M0S)")
                continue

            # Parse days of week
            days_elem = vj.find('.//txc:DaysOfWeek', namespaces=NS)
            days_bitmask = days_to_bitmask(days_elem)

            # Calculate arrival/departure times by accumulating run times
            dep_parts = departure_str.split(':')
            base_hour, base_min, base_sec = int(dep_parts[0]), int(
                dep_parts[1]), int(dep_parts[2]) if len(dep_parts) > 2 else 0
            cumulative_secs = base_hour * 3600 + base_min * 60 + base_sec

            for stop_ref, seq, run_time in sections[section_id]:
                # Accumulate the travel time to reach this stop first,
                # so arrival_secs reflects when the bus arrives here.
                cumulative_secs += run_time
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
