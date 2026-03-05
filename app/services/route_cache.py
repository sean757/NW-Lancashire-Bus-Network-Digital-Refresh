"""
Route cache service — loads route data into memory for fast journey planning.
Refreshes every hour.
"""

import asyncio
import re
from sqlalchemy import text
from app.database import async_session


class RouteCache:
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
        self._loaded = False

    async def load(self):
        """Load all route and stop data into memory."""
        async with async_session() as db:
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

        self._loaded = True
        print(
            f"Route cache loaded: {len(self.routes)} routes, {len(self.stop_routes)} stops with routes")

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
        import math

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
