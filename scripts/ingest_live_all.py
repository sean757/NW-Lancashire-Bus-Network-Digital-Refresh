import requests
import psycopg2
import time
from lxml import etree
from datetime import datetime
import urllib3

# Silence SSL warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 1. Database Configuration [cite: 132]
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "transport",
    "password": "transport_dev",
    "dbname": "transport_db"
}

# 2. Operators from your documentation
OPERATORS = ["ARCT", "BLAC", "KLCO", "SCCU", "SCMY", "NUTT"]

# XML Namespace used in the SIRI feed (from your screenshot)
NS = {'siri': 'http://www.siri.org.uk/siri'}


# Removed setup_database() - schema is managed by init_db.sql


def fetch_and_store_live_data(conn):
    """Fetch and parse SIRI XML live data """
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'application/xml, text/xml'  # Request XML instead of JSON
    }
    total_updated = 0

    with conn.cursor() as cur:
        for noc in OPERATORS:
            url = f"https://transport.scc.lancs.ac.uk/bus/live/{noc}"
            try:
                response = requests.get(
                    url, headers=headers, verify=False, timeout=10)
                if response.status_code != 200:
                    continue

                # Parse the XML content
                root = etree.fromstring(response.content)

                # Find all VehicleActivity elements using the namespace
                activities = root.xpath(
                    './/siri:VehicleActivity', namespaces=NS)

                insert_query = """
                    INSERT INTO live_positions (vehicle_id, latitude, longitude, bearing, source, recorded_at)
                    VALUES (%s, %s, %s, %s, %s, %s);
                """

                count = 0
                for activity in activities:
                    # Navigate the XML tree based on your screenshot
                    journey = activity.find(
                        './/siri:MonitoredVehicleJourney', namespaces=NS)
                    if journey is not None:
                        vehicle_id = journey.findtext(
                            'siri:VehicleRef', namespaces=NS)
                        operator = journey.findtext(
                            'siri:OperatorRef', namespaces=NS)
                        lat = journey.findtext(
                            './/siri:Latitude', namespaces=NS)
                        lon = journey.findtext(
                            './/siri:Longitude', namespaces=NS)
                        bearing = journey.findtext(
                            'siri:Bearing', namespaces=NS) or 0

                        if vehicle_id and lat and lon:
                            cur.execute(
                                insert_query, (vehicle_id, lat, lon, bearing, 'api', datetime.now()))
                            count += 1
                            total_updated += 1

                if count > 0:
                    print(f"   {noc}: Updated {count} vehicles")

            except Exception as e:
                print(f"  Error processing {noc}: {e}")

        conn.commit()
    print(f"[{datetime.now().strftime('%H:%M:%S')}]  Scan complete. Total vehicles: {total_updated}\n")


if __name__ == "__main__":
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        # Removed setup_database(conn) - run init_db.sql first
        print("Starting Live SIRI XML Tracking (10s interval) [cite: 138]\n")

        while True:
            fetch_and_store_live_data(conn)
            time.sleep(10)  # 10 second refresh interval

    except KeyboardInterrupt:
        print("\n Stopped.")
    finally:
        if 'conn' in locals() and conn:
            conn.close()
