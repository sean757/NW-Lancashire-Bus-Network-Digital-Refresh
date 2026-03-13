import os
import requests
from lxml import etree
import psycopg2

# 1. Database configuration (using the IP you successfully tested earlier)
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "transport",
    "password": "transport_dev",
    "dbname": "transport_db"
}

"""Ingest NaPTAN stops into Postgres.

Root cause of "no stops in central Blackpool":
- The stops table only contained ATCO area code 250 (Lancashire; stop IDs
    beginning 2500...), so Blackpool Borough stops (ATCO area code 259; stop IDs
    beginning 2590...) weren't present.

We can fix this by ingesting a national NaPTAN dataset. The project already has
access to one via the SCC transport API.
"""

# SCC-hosted national NaPTAN dataset (large); streamed parsing keeps memory usage low.
NAPTAN_FULL_URL = os.getenv(
    "NAPTAN_FULL_URL",
    "https://transport.scc.lancs.ac.uk/nptg/naptan-full.xml",
)


# Removed create_table() - schema is managed by init_db.sql


def ingest_data(conn):
    """Download and parse NaPTAN XML using lxml.iterparse (streaming)."""
    print(
        f" Downloading and parsing NaPTAN data from {NAPTAN_FULL_URL}, please wait...")

    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64)",
        "Accept": "application/xml, text/xml",
    }

    response = requests.get(NAPTAN_FULL_URL, stream=True,
                            headers=headers, timeout=120)
    response.raise_for_status()
    response.raw.decode_content = True

    # The XML uses Namespaces, which must be included in the tag for iterparse.
    context = etree.iterparse(
        response.raw,
        events=("end",),
        tag="{http://www.naptan.org.uk/}StopPoint",
    )

    insert_query = """
        INSERT INTO stops (stop_id, stop_name, locality, latitude, longitude, active)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (stop_id) DO UPDATE SET
            stop_name = EXCLUDED.stop_name,
            locality = EXCLUDED.locality,
            latitude = EXCLUDED.latitude,
            longitude = EXCLUDED.longitude,
            active = EXCLUDED.active;
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

            status = (elem.get("Status") or "").strip().lower()
            active = status != "inactive"

            if atco_code and name and lat and lon:
                cur.execute(insert_query, (atco_code, name,
                            locality, lat, lon, active))
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
        # Removed create_table(conn) - run init_db.sql first
        ingest_data(conn)
        conn.close()
    except Exception as e:
        print(f"❌ An error occurred: {e}")
