import requests
import psycopg2
import zipfile
import io
from lxml import etree
import urllib3
from datetime import datetime

# Silence SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DB_CONFIG = {
    "host": "172.17.0.1",
    "port": "5432",
    "user": "transport",
    "password": "transport_dev",
    "dbname": "transport_db"
}

# Discovery URL from your screenshot
DISCOVERY_URL = "https://transport.scc.lancs.ac.uk/bus/times/SCCU"


def setup_timetable_schema(conn):
    """Create tables as per Design Report requirements[cite: 142]."""
    with conn.cursor() as cur:
        # Table for Bus route definitions [cite: 142]
        cur.execute("""
            CREATE TABLE IF NOT EXISTS routes (
                id SERIAL PRIMARY KEY,
                service_code VARCHAR(100),
                line_name VARCHAR(50),
                stop_atco_code VARCHAR(20),
                sequence_number INTEGER
            );
        """)
        # Table for Scheduled departure times [cite: 142]
        cur.execute("""
            CREATE TABLE IF NOT EXISTS timetables (
                id SERIAL PRIMARY KEY,
                service_code VARCHAR(100),
                stop_atco_code VARCHAR(20),
                departure_time TIME,
                journey_code VARCHAR(100)
            );
        """)
        conn.commit()
        print(" Database schema updated for Timetables and Routes.")


def parse_txc_xml(conn, xml_content):
    """Parse TransXChange XML using iterparse for memory efficiency."""
    NS = {'txc': 'http://www.transxchange.org.uk/'}
    context = etree.iterparse(io.BytesIO(xml_content), events=(
        'end',), tag='{http://www.transxchange.org.uk/}Service')

    with conn.cursor() as cur:
        for event, elem in context:
            service_code = elem.findtext('txc:ServiceCode', namespaces=NS)
            line_name = elem.findtext('.//txc:LineName', namespaces=NS)

            # Logic to extract StopPoints and JourneyTimes would be added here
            # For now, let's track the services we found
            if service_code and line_name:
                print(f"    Found Service: {line_name} ({service_code})")

            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]
        conn.commit()


def download_and_extract(conn, zip_url):
    """Download ZIP and process contained XML files."""
    print(f"   Downloading ZIP from: {zip_url}")
    try:
        response = requests.get(zip_url, verify=False, timeout=30)
        with zipfile.ZipFile(io.BytesIO(response.content)) as z:
            # Process every XML file inside the ZIP
            for filename in z.namelist():
                if filename.endswith('.xml'):
                    print(f"    📄 Parsing XML: {filename}")
                    parse_txc_xml(conn, z.read(filename))
    except Exception as e:
        print(f"   Error processing ZIP: {e}")


def run_ingestion():
    """Main execution logic using results from Discovery API."""
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        setup_timetable_schema(conn)

        print(f" Fetching discovery data from {DISCOVERY_URL}...")
        response = requests.get(DISCOVERY_URL, verify=False)
        data = response.json()

        # Access the 'results' list from your screenshot
        results = data.get('results', [])
        print(f"Found {len(results)} dataset records.\n")

        for item in results:
            # Get the download URL and extension from the JSON
            zip_url = item.get('url')
            ext = item.get('extension')

            if zip_url and ext == "zip":
                download_and_extract(conn, zip_url)

        conn.close()
        print("\n All timetable data processed successfully!")

    except Exception as e:
        print(f" Ingestion failed: {e}")


if __name__ == "__main__":
    run_ingestion()
