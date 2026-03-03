import xml.etree.ElementTree as ET
import psycopg2
import json
import os
import gzip
import requests

# Database connection details from project documentation
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "transport",
    "password": "transport_dev",
    "dbname": "transport_db"
}

# Updated to use the 548MB full UK dataset
URLS = {
    "naptan_full.xml": "https://transport.scc.lancs.ac.uk/nptg/naptan-full.xml",
    "corpus.json": "https://transport.scc.lancs.ac.uk/rail/corpus"
}


def download_large_file(url, filename):
    if not os.path.exists(filename):
        print(f"Downloading large dataset {filename} (approx 548MB)...")
        try:
            # Increased timeout for large file stream
            response = requests.get(url, stream=True, timeout=60)
            response.raise_for_status()
            with open(filename, 'wb') as f:
                # 1MB chunks
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
            print(f"Successfully downloaded {filename}")
        except Exception as e:
            print(f"Download failed: {e}. Ensure VPN is active.")
            return False
    return True


def parse_full_rail_naptan(xml_file):
    if not os.path.exists(xml_file):
        return

    print(f"Processing full UK NaPTAN data from {xml_file}...")
    conn = psycopg2.connect(**DB_CONFIG)
    cursor = conn.cursor()

    # Optional: Clear old data before full import
    cursor.execute("DELETE FROM rail_stops;")

    count = 0
    # Streaming parse is mandatory for 548MB file to avoid memory overflow
    context = ET.iterparse(xml_file, events=("end",))

    for event, elem in context:
        tag = elem.tag.split('}')[-1]
        if tag == 'StopPoint':
            st_type_elem = elem.find('.//{*}StopType')
            st_type = st_type_elem.text if st_type_elem is not None else ""

            # Filter for RLY (Station) and RSE (Entrance) [cite: 728, 769]
            if st_type in ['RLY', 'RSE']:
                try:
                    atco = elem.find('.//{*}AtcoCode').text
                    name = elem.find('.//{*}CommonName').text
                    lon = float(elem.find('.//{*}Longitude').text)
                    lat = float(elem.find('.//{*}Latitude').text)

                    cursor.execute('''
                        INSERT INTO rail_stops (atco_code, common_name, stop_type, longitude, latitude)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (atco_code) DO UPDATE SET common_name = EXCLUDED.common_name;
                    ''', (atco, name, st_type, lon, lat))
                    count += 1

                    # Periodic commit every 1000 records
                    if count % 1000 == 0:
                        conn.commit()
                        print(f"Progress: Imported {count} stations...")
                except (AttributeError, ValueError):
                    pass

            # CRITICAL: Clear the element to free memory
            elem.clear()

    conn.commit()
    cursor.close()
    conn.close()
    print(f"Full import finished. Total rail nodes inserted: {count}")


if __name__ == "__main__":
    # Ensure you have enough disk space (approx 600MB for XML + DB)
    if download_large_file(URLS["naptan_full.xml"], "naptan_full.xml"):
        parse_full_rail_naptan("naptan_full.xml")
