import psycopg2
from datetime import datetime
import requests


def get_direct_journeys(conn, origin_stop_id: str, dest_stop_id: str, request_datetime: datetime):
    """
    Tasks 3 & 6: Find direct routes between two stops and filter them by time/date.

    :param conn: psycopg2 database connection object
    :param origin_stop_id: The ATCOCode of the starting stop
    :param dest_stop_id: The ATCOCode of the destination stop
    :param request_datetime: The datetime object representing when the user wants to depart
    :return: A list of dictionaries containing valid journey options
    """

    # 1. Parse date and time from the request (Task 6)
    req_date = request_datetime.date()
    req_time = request_datetime.time()

    # 2. Calculate the days_of_week bitmask (Task 6)
    # Mon=1, Tue=2, Wed=4, Thu=8, Fri=16, Sat=32, Sun=64
    day_mapping = [1, 2, 4, 8, 16, 32, 64]
    current_day_bitmask = day_mapping[request_datetime.weekday()]

    # 3. Build the SQL Query combining Task 3 (Matching) and Task 6 (Filtering)
    query = """
        SELECT 
            r.route_name,
            r.operator,
            t_orig.trip_id,
            t_orig.departure_time AS origin_dep_time,
            t_dest.arrival_time AS dest_arr_time
        FROM route_stops rs_orig
        
        -- Match the destination stop on the same route and direction (Task 3)
        JOIN route_stops rs_dest 
          ON rs_orig.route_id = rs_dest.route_id 
         AND rs_orig.direction = rs_dest.direction
         
        JOIN routes r ON rs_orig.route_id = r.route_id
          
        -- Join timetable for the origin stop
        JOIN timetables t_orig 
          ON rs_orig.route_id = t_orig.route_id 
         AND rs_orig.stop_id = t_orig.stop_id 
         AND rs_orig.direction = t_orig.direction
         
        -- Join timetable for the destination stop (matching trip_id ensures it's the same bus)
        JOIN timetables t_dest 
          ON rs_dest.route_id = t_dest.route_id 
         AND rs_dest.stop_id = t_dest.stop_id 
         AND rs_dest.direction = t_dest.direction 
         AND t_orig.trip_id = t_dest.trip_id
         
        WHERE rs_orig.stop_id = %s 
          AND rs_dest.stop_id = %s
          
          -- Ensure origin comes before destination (Task 3)
          AND rs_orig.stop_sequence < rs_dest.stop_sequence 
          
          -- Time-aware filtering (Task 6)
          AND t_orig.departure_time >= %s                    -- Bus departs after requested time
          AND (t_orig.days_of_week & %s) > 0                 -- Bus runs on this day of the week
          AND (t_orig.valid_from IS NULL OR t_orig.valid_from <= %s)   -- Route is valid today
          AND (t_orig.valid_until IS NULL OR t_orig.valid_until >= %s)
          
        -- Sort by earliest arrival (Task 6)
        ORDER BY t_dest.arrival_time ASC 
        LIMIT 10;
    """

    journeys = []
    try:
        with conn.cursor() as cur:
            cur.execute(query, (
                origin_stop_id, dest_stop_id,
                req_time, current_day_bitmask,
                req_date, req_date
            ))

            for row in cur.fetchall():
                journeys.append({
                    "route_name": row[0],
                    "operator": row[1],
                    "trip_id": row[2],
                    # Convert psycopg2 time objects to strings
                    "departure_time": row[3].strftime('%H:%M:%S'),
                    "arrival_time": row[4].strftime('%H:%M:%S')
                })
    except Exception as e:
        print(f"Error querying database for direct journeys: {e}")

    return journeys


def get_walking_segment(start_lon: float, start_lat: float, end_lon: float, end_lat: float, api_key: str):
    """
    Task 5: Call OpenRouteService API for walking directions.

    :param start_lon: Longitude of the starting point
    :param start_lat: Latitude of the starting point
    :param end_lon: Longitude of the destination point
    :param end_lat: Latitude of the destination point
    :param api_key: OpenRouteService API Key
    :return: A dictionary containing distance, duration, and route geometry, or None if failed.
    """
    url = "https://api.openrouteservice.org/v2/directions/foot-walking"
    headers = {
        'Authorization': api_key,
        'Content-Type': 'application/json'
    }

    # ORS API expects coordinates in [Longitude, Latitude] format
    body = {
        "coordinates": [[start_lon, start_lat], [end_lon, end_lat]]
    }

    try:
        response = requests.post(url, json=body, headers=headers, timeout=5)

        if response.status_code == 200:
            data = response.json()
            route = data['routes'][0]

            return {
                "type": "walk",
                "distance_meters": route['summary']['distance'],
                "duration_seconds": route['summary']['duration'],
                # Useful for drawing the line on a frontend map
                "geometry": route['geometry']
            }
        else:
            print(
                f"ORS API Error: Status {response.status_code}, {response.text}")
            return None

    except Exception as e:
        print(f"Failed to fetch walking segment from ORS: {e}")
        return None
