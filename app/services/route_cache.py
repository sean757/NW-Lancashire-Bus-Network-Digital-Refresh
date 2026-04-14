"""
Route cache service — loads route data into memory for fast journey planning.
Refreshes every hour.
"""

import asyncio
import bisect
import heapq
import re
from datetime import date, time as dtime
from sqlalchemy import text
from app.database import async_session


class RouteCache:
    # Maximum journey duration accepted by the multi-transfer planner (seconds).
    MAX_JOURNEY_SECS = 4 * 3600  # 4 hours

    def __init__(self):
        # route_id -> {route_name, operator, route_type}
        self.routes = {}
        # route_id -> [(stop_id, sequence, direction), ...]
        self.route_stops = {}
        # stop_id -> [(route_id, sequence, direction), ...]
        self.stop_routes = {}
        # stop_id -> (latitude, longitude)
        self.stop_coords = {}
        # stop_id -> stop_name
        self.stop_names = {}

        # ---- Timetable data for multi-transfer routing ----
        # trip_id -> [(stop_id, seq, arr_secs, dep_secs)] sorted by seq
        self.trip_stops_sorted = {}
        # (trip_id, stop_id) -> arr_secs  [fast O(1) arrival lookup]
        self.trip_arrival_at = {}
        # (trip_id, stop_id) -> (seq, arr_secs, dep_secs)
        self.trip_stop_seq = {}
        # (route_id, direction, stop_id) -> sorted [(dep_secs, trip_id)]
        self.stop_departures = {}
        # trip_id -> {route_id, direction, days_of_week, valid_from, valid_until}
        self.trip_meta = {}
        # stop_id -> [(other_stop_id, walk_secs)]  (pre-computed foot-path graph)
        self.foot_paths = {}

        self._loaded = False

    async def load(self):
        """Load all route, stop, and timetable data into memory."""
        async with async_session() as db:
            # Set a statement timeout so a stuck lock can't block indefinitely.
            await db.execute(text("SET statement_timeout = '120s'"))

            # Load routes
            result = await db.execute(text(
                "SELECT route_id, route_name, operator, route_type FROM routes WHERE active = TRUE"
            ))
            self.routes = {}
            for row in result.mappings():
                self.routes[row["route_id"]] = dict(row)

            # Load route_stops
            result = await db.execute(text(
                "SELECT route_id, stop_id, stop_sequence, direction FROM route_stops ORDER BY route_id, stop_sequence"
            ))
            self.route_stops = {}
            self.stop_routes = {}
            for row in result.mappings():
                rid = row["route_id"]
                sid = row["stop_id"]
                seq = row["stop_sequence"]
                direction = row["direction"]

                if rid not in self.route_stops:
                    self.route_stops[rid] = []
                self.route_stops[rid].append((sid, seq, direction))

                if sid not in self.stop_routes:
                    self.stop_routes[sid] = []
                self.stop_routes[sid].append((rid, seq, direction))

            # Load stop coordinates and names for nearby lookups
            result = await db.execute(text(
                "SELECT stop_id, latitude, longitude, stop_name FROM stops WHERE active = TRUE"
            ))
            self.stop_coords = {}
            self.stop_names = {}
            for row in result.mappings():
                self.stop_coords[row["stop_id"]] = (
                    row["latitude"], row["longitude"])
                self.stop_names[row["stop_id"]] = row["stop_name"]

            # Load timetable data for multi-transfer routing (RAPTOR / Dijkstra)
            result = await db.execute(text("""
                SELECT route_id, stop_id, trip_id,
                       (EXTRACT(HOUR FROM arrival_time)*3600
                        + EXTRACT(MINUTE FROM arrival_time)*60
                        + EXTRACT(SECOND FROM arrival_time))::integer  AS arr_secs,
                       (EXTRACT(HOUR FROM departure_time)*3600
                        + EXTRACT(MINUTE FROM departure_time)*60
                        + EXTRACT(SECOND FROM departure_time))::integer AS dep_secs,
                       stop_sequence, direction,
                       days_of_week, valid_from, valid_until
                FROM timetables
                ORDER BY trip_id, stop_sequence
            """))

            self.trip_stops_sorted = {}
            self.trip_arrival_at = {}
            self.trip_stop_seq = {}
            self.stop_departures = {}
            self.trip_meta = {}

            for row in result.mappings():
                tid = row["trip_id"]
                rid = row["route_id"]
                sid = row["stop_id"]
                d = row["direction"]
                seq = row["stop_sequence"]
                arr = int(row["arr_secs"])
                dep = int(row["dep_secs"])

                if tid not in self.trip_stops_sorted:
                    self.trip_stops_sorted[tid] = []
                self.trip_stops_sorted[tid].append((sid, seq, arr, dep))

                self.trip_arrival_at[(tid, sid)] = arr
                self.trip_stop_seq[(tid, sid)] = (seq, arr, dep)

                if tid not in self.trip_meta:
                    self.trip_meta[tid] = {
                        "route_id": rid,
                        "direction": d,
                        "days_of_week": row["days_of_week"],
                        "valid_from": row["valid_from"],
                        "valid_until": row["valid_until"],
                    }

                dep_key = (rid, d, sid)
                if dep_key not in self.stop_departures:
                    self.stop_departures[dep_key] = []
                self.stop_departures[dep_key].append((dep, tid))

        # Sort trip stops and departure lists once
        for tid in self.trip_stops_sorted:
            self.trip_stops_sorted[tid].sort(key=lambda x: x[1])
        for key in self.stop_departures:
            self.stop_departures[key].sort()

        # Pre-compute walking connections between nearby stops
        self._compute_foot_paths()

        self._loaded = True
        print(
            f"Route cache loaded: {len(self.routes)} routes, "
            f"{len(self.stop_routes)} stops with routes, "
            f"{len(self.trip_meta)} trips"
        )

    async def refresh_loop(self, interval_secs=3600):
        """Background task to refresh cache periodically."""
        while True:
            await self.load()
            await asyncio.sleep(interval_secs)

    def get_routes_for_stop(self, stop_id):
        """Return list of (route_id, sequence, direction) for a given stop."""
        return self.stop_routes.get(stop_id, [])

    def get_stops_for_route(self, route_id, direction=None):
        """Return ordered list of (stop_id, sequence, direction) for a route."""
        stops = self.route_stops.get(route_id, [])
        if direction:
            stops = [s for s in stops if s[2] == direction]
        return sorted(stops, key=lambda x: x[1])

    def get_common_routes(self, stop_a, stop_b):
        """Find routes that serve both stops, with stop_a before stop_b."""
        routes_a = self.get_routes_for_stop(stop_a)
        routes_b = self.get_routes_for_stop(stop_b)

        # Index routes for stop_b by (route_id, direction)
        b_index = {}
        for rid, seq, direction in routes_b:
            b_index[(rid, direction)] = seq

        matches = []
        for rid, seq_a, direction in routes_a:
            key = (rid, direction)
            if key in b_index:
                seq_b = b_index[key]
                if seq_a < seq_b:  # Correct order
                    matches.append({
                        "route_id": rid,
                        "route_name": self.routes.get(rid, {}).get("route_name", ""),
                        "operator": self.routes.get(rid, {}).get("operator", ""),
                        "direction": direction,
                        "origin_sequence": seq_a,
                        "destination_sequence": seq_b,
                    })

        return matches

    def find_transfer_routes(self, stop_a, stop_b, max_walk_km=0.5):
        """Find routes with one transfer between stop_a and stop_b.
        Looks for intermediate stops within walking distance that connect two routes."""
        routes_from_a = self.get_routes_for_stop(stop_a)
        routes_to_b = self.get_routes_for_stop(stop_b)

        # Build set of (route_id, direction) -> sequence for destination
        b_route_index = {}
        for rid, seq, direction in routes_to_b:
            if (rid, direction) not in b_route_index:
                b_route_index[(rid, direction)] = seq

        transfers = []

        for rid_a, seq_a, dir_a in routes_from_a:
            # Get all stops on this route AFTER stop_a
            route_stops = self.get_stops_for_route(rid_a, dir_a)
            for mid_stop_id, mid_seq, _ in route_stops:
                if mid_seq <= seq_a:
                    continue  # Must be after origin

                # Check if any stop near mid_stop has a route to stop_b
                mid_coords = self.stop_coords.get(mid_stop_id)
                if not mid_coords:
                    continue

                # Find nearby stops to transfer at
                for candidate_stop, coords in self.stop_coords.items():
                    if candidate_stop == mid_stop_id:
                        dist = 0.0
                    else:
                        dist = self._haversine(
                            mid_coords[0], mid_coords[1], coords[0], coords[1])

                    if dist > max_walk_km:
                        continue

                    # Check if candidate_stop has a route to stop_b
                    for rid_b, seq_transfer, dir_b in self.get_routes_for_stop(candidate_stop):
                        key = (rid_b, dir_b)
                        if key in b_route_index and b_route_index[key] > seq_transfer:
                            transfers.append({
                                "leg1_route_id": rid_a,
                                "leg1_route_name": self.routes.get(rid_a, {}).get("route_name", ""),
                                "leg1_direction": dir_a,
                                "leg1_origin_seq": seq_a,
                                "leg1_alight_stop": mid_stop_id,
                                "leg1_alight_seq": mid_seq,
                                "walk_distance_km": round(dist, 3),
                                "transfer_stop": candidate_stop,
                                "leg2_route_id": rid_b,
                                "leg2_route_name": self.routes.get(rid_b, {}).get("route_name", ""),
                                "leg2_direction": dir_b,
                                "leg2_board_seq": seq_transfer,
                                "leg2_destination_seq": b_route_index[key],
                            })

                # Limit search to avoid combinatorial explosion
                if len(transfers) >= 20:
                    return transfers

        return transfers

    def _compute_foot_paths(self, max_walk_km: float = 0.5):
        """Pre-compute walking connections between stops within *max_walk_km*.

        Uses a spatial grid (0.01° ≈ 1 km cells) so that only stops in
        adjacent grid cells are compared, keeping the computation fast even
        for networks with thousands of stops.
        """
        CELL_SIZE = 0.01  # degrees per grid cell (~1 km)
        WALK_SPEED_KMH = 5.0

        # Build grid index
        grid: dict = {}
        for stop_id, (lat, lon) in self.stop_coords.items():
            cell = (int(lat / CELL_SIZE), int(lon / CELL_SIZE))
            grid.setdefault(cell, []).append(stop_id)

        foot_paths: dict = {}
        for stop_id, (lat, lon) in self.stop_coords.items():
            cell_lat = int(lat / CELL_SIZE)
            cell_lon = int(lon / CELL_SIZE)

            neighbours = []
            for dlat in range(-1, 2):
                for dlon in range(-1, 2):
                    neighbours.extend(
                        grid.get((cell_lat + dlat, cell_lon + dlon), []))

            foot_paths[stop_id] = []
            for other_id in neighbours:
                if other_id == stop_id:
                    continue
                other_lat, other_lon = self.stop_coords[other_id]
                dist = self._haversine(lat, lon, other_lat, other_lon)
                if dist <= max_walk_km:
                    walk_secs = int(dist / WALK_SPEED_KMH * 3600)
                    foot_paths[stop_id].append((other_id, walk_secs))

        self.foot_paths = foot_paths

    def _earliest_trip_from_stop(
        self,
        route_id: str,
        direction: str,
        stop_id: str,
        from_secs: int,
        day_mask: int,
        dep_date: date,
    ):
        """Return (trip_id, dep_secs) for the earliest valid trip on
        (*route_id*, *direction*) from *stop_id* departing no earlier than
        *from_secs*, or ``None`` if no such trip exists."""
        dep_key = (route_id, direction, stop_id)
        trips = self.stop_departures.get(dep_key)
        if not trips:
            return None

        idx = bisect.bisect_left(trips, (from_secs,))
        # Scan at most this many candidate trips to avoid excessive iteration
        # when many trips depart close together on the same day/validity range.
        _MAX_CANDIDATE_TRIPS = 30
        for dep_s, tid in trips[idx:idx + _MAX_CANDIDATE_TRIPS]:
            meta = self.trip_meta.get(tid, {})
            if not (meta.get("days_of_week", 127) & day_mask):
                continue
            vf = meta.get("valid_from")
            vu = meta.get("valid_until")
            if vf and vf > dep_date:
                continue
            if vu and vu < dep_date:
                continue
            return tid, dep_s

        return None

    def plan_multi_transfer(
        self,
        origin: str,
        destination: str,
        dep_time: dtime,
        dep_date: date,
        max_transfers: int = 5,
    ) -> list:
        """Plan journeys with up to *max_transfers* bus-to-bus transfers.

        Uses a time-dependent Dijkstra over the in-memory timetable.  Each
        "state" is a (stop_id, num_bus_legs) pair; the priority is earliest
        arrival time.  Walking between nearby stops is allowed at any point
        without consuming a transfer slot.

        Returns a list of journey paths.  Each path is a list of leg dicts:
          Bus leg  – keys: type, route_id, route_name, operator, direction,
                           trip_id, board_stop, board_secs, alight_stop,
                           alight_secs
          Walk leg – keys: type, from_stop, to_stop, walk_secs
        """
        if not self._loaded:
            return []

        INF = float("inf")
        day_mask = 1 << dep_date.weekday()
        dep_secs = dep_time.hour * 3600 + dep_time.minute * 60 + dep_time.second

        # best[(stop_id, num_bus_legs)] = earliest arrival_secs reached so far
        best: dict = {}
        best[(origin, 0)] = dep_secs

        # Priority queue items: (arrival_secs, num_bus_legs, stop_id, path)
        # path is a list of leg dicts (bounded at ~2*max_transfers items)
        pq = [(dep_secs, 0, origin, [])]

        results: list = []

        while pq:
            curr_secs, num_legs, curr_stop, legs = heapq.heappop(pq)

            # Reached destination — record result
            if curr_stop == destination and num_legs > 0:
                results.append(legs)
                if len(results) >= 3:
                    break
                continue

            # Pruning: exceeded maximum transfers
            if num_legs > max_transfers:
                continue

            # Pruning: journey too long
            if curr_secs - dep_secs > self.MAX_JOURNEY_SECS:
                continue

            # Pruning: a better path to this state was already processed
            state = (curr_stop, num_legs)
            if best.get(state, INF) < curr_secs:
                continue

            # ---- Explore bus routes departing from curr_stop ----
            for route_id, _seq_at_curr, direction in self.get_routes_for_stop(curr_stop):
                result = self._earliest_trip_from_stop(
                    route_id, direction, curr_stop, curr_secs, day_mask, dep_date
                )
                if result is None:
                    continue
                trip_id, _trip_dep_secs = result

                # Find the boarding stop's position in this trip
                board_info = self.trip_stop_seq.get((trip_id, curr_stop))
                if board_info is None:
                    continue
                board_seq = board_info[0]

                # Traverse all stops after the boarding stop on this trip
                for stop_id, seq, arr_secs, _dep_secs in self.trip_stops_sorted.get(trip_id, []):
                    if seq <= board_seq:
                        continue
                    if arr_secs < curr_secs:
                        continue  # safety: should not happen for valid data

                    new_state = (stop_id, num_legs + 1)
                    if arr_secs < best.get(new_state, INF):
                        best[new_state] = arr_secs
                        new_leg = {
                            "type": "bus",
                            "route_id": route_id,
                            "route_name": self.routes.get(route_id, {}).get("route_name", ""),
                            "operator": self.routes.get(route_id, {}).get("operator", ""),
                            "direction": direction,
                            "trip_id": trip_id,
                            "board_stop": curr_stop,
                            "board_secs": curr_secs,
                            "alight_stop": stop_id,
                            "alight_secs": arr_secs,
                        }
                        heapq.heappush(
                            pq, (arr_secs, num_legs + 1, stop_id, legs + [new_leg]))

            # ---- Walking transfers (free, don't consume a transfer slot) ----
            for other_stop, walk_secs in self.foot_paths.get(curr_stop, []):
                walk_arr = curr_secs + walk_secs
                walk_state = (other_stop, num_legs)
                if walk_arr < best.get(walk_state, INF):
                    best[walk_state] = walk_arr
                    new_walk_leg = {
                        "type": "walk",
                        "from_stop": curr_stop,
                        "to_stop": other_stop,
                        "walk_secs": walk_secs,
                    }
                    heapq.heappush(
                        pq, (walk_arr, num_legs, other_stop, legs + [new_walk_leg]))

        return results

    @staticmethod
    def _get_base_name(name: str) -> str:
        """Strip a trailing letter/number suffix to get the base stop name.

        Handles cases like:
          'Common Garden Street A' -> 'Common Garden Street'
          'Market Street (Stop B)' -> 'Market Street'
          'Bus Station 1'          -> 'Bus Station'
        Falls back to the original name if stripping would leave an empty string.
        """
        # Remove trailing parenthetical suffix, e.g. ' (Stop A)' or ' (adj School)'
        cleaned = re.sub(r'\s*\([^)]*\)\s*$', '', name).strip()
        # Remove trailing single uppercase letter or digit (NaPTAN convention, e.g. ' A', ' B', ' 1')
        trimmed = re.sub(r'\s+[A-Z0-9]$', '', cleaned).strip()
        return trimmed if trimmed else name

    def find_similar_stops(self, stop_id: str, radius_km: float = 0.2) -> list:
        """Return stop IDs within *radius_km* that share the same base name as *stop_id*.

        This is used to handle duplicate stops such as 'Common Garden Street A',
        'Common Garden Street B', and 'Common Garden Street C', which represent
        physically adjacent stops for the same location.  Including all of them
        during journey planning ensures that routes only serving stop B or C are
        not missed when the user has selected stop A.

        The original *stop_id* is always the first element of the returned list.
        """
        coords = self.stop_coords.get(stop_id)
        name = self.stop_names.get(stop_id)
        if not coords or not name:
            return [stop_id]

        base = self._get_base_name(name)
        similar = [stop_id]

        for other_id, other_coords in self.stop_coords.items():
            if other_id == stop_id:
                continue
            dist = self._haversine(
                coords[0], coords[1], other_coords[0], other_coords[1])
            if dist > radius_km:
                continue
            other_name = self.stop_names.get(other_id, '')
            if self._get_base_name(other_name) == base:
                similar.append(other_id)

        return similar

    @staticmethod
    def _haversine(lat1, lon1, lat2, lon2):
        """Calculate distance in km between two coordinates."""
        import math
        R = 6371
        dlat = math.radians(lat2 - lat1)
        dlon = math.radians(lon2 - lon1)
        a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * \
            math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
        return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# Singleton instance
route_cache = RouteCache()
