import requests
from lxml import etree
import psycopg2

# 1. Database configuration (using the IP you successfully tested earlier)
DB_CONFIG = {
    "host": "172.17.0.1",
    "port": 5432,
    "user": "transport",
    "password": "transport_dev",
    "dbname": "transport_db"
}

# 2. The XML data URL we found earlier
URL = "https://transport.scc.lancs.ac.uk/nptg/naptan.xml"


def create_table(conn):
    """Create the Stops table based on the Design Report requirements."""
    with conn.cursor() as cur:
        # Create Stops table containing atco_code, name, coordinates, and locality
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stops (
                atco_code VARCHAR(20) PRIMARY KEY,
                stop_name VARCHAR(255),
                locality VARCHAR(50),
                latitude NUMERIC,
                longitude NUMERIC
            );
        """)
        conn.commit()
        print(" Database table 'stops' checked/created successfully.")


def ingest_data(conn):
    """Efficiently download and parse XML using lxml.iterparse."""
    print(f" Downloading and parsing data from {URL}, please wait...")

    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }

    # Use requests to stream the download of the large file
    response = requests.get(URL, stream=True, headers=headers)
    response.raise_for_status()

    response.raw.decode_content = True

    # The XML uses Namespaces, which must be included in the tag for iterparse.
    context = etree.iterparse(response.raw, events=(
        'end',), tag='{http://www.naptan.org.uk/}StopPoint')

    insert_query = """
        INSERT INTO stops (atco_code, stop_name, locality, latitude, longitude)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (atco_code) DO NOTHING;
    """

    count = 0
    with conn.cursor() as cur:
        for event, elem in context:
            # Extract the necessary data
            atco_code = elem.findtext('.//{http://www.naptan.org.uk/}AtcoCode')
            name = elem.findtext('.//{http://www.naptan.org.uk/}CommonName')
            locality = elem.findtext(
                './/{http://www.naptan.org.uk/}NptgLocalityRef')
            lat = elem.findtext('.//{http://www.naptan.org.uk/}Latitude')
            lon = elem.findtext('.//{http://www.naptan.org.uk/}Longitude')

            if atco_code and name and lat and lon:
                cur.execute(insert_query, (atco_code,
                            name, locality, lat, lon))
                count += 1

            # Clean up memory
            elem.clear()
            while elem.getprevious() is not None:
                del elem.getparent()[0]

            # Print progress every 1000 records
            if count > 0 and count % 1000 == 0:
                print(f"Successfully imported {count} stops...")

        conn.commit()
    print(
        f" Import complete! A total of {count} stops have been saved to the database!")


if __name__ == "__main__":
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        create_table(conn)
        ingest_data(conn)
        conn.close()
    except Exception as e:
        print(f"❌ An error occurred: {e}")
