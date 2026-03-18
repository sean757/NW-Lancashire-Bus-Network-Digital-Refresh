"""
Vehicle cache service — polls the SCC University SIRI feed every 30 seconds
and holds the latest parsed vehicle positions in memory.

Instead of every user request triggering upstream calls to the SCC API, the
backend fetches data once per interval and all client requests are served from
the in-memory cache.  This prevents large numbers of concurrent users from
generating excessive traffic to the upstream API.
"""

import asyncio
import logging
from datetime import datetime, timezone

import httpx
from lxml import etree

logger = logging.getLogger(__name__)

_SIRI_NS = {'siri': 'http://www.siri.org.uk/siri'}
_OPERATORS = ["ARCT", "BLAC", "KLCO", "SCCU", "SCMY", "NUTT"]

# How often (seconds) the background task refreshes the cache.
# Matches the frontend polling interval so clients always receive
# reasonably fresh data without adding extra upstream load.
REFRESH_INTERVAL_SECS = 30


class VehicleCache:
    """In-memory cache of live vehicle positions fetched from the SIRI feed."""

    def __init__(self):
        # List of vehicle dicts populated by the background refresh task.
        self.vehicles: list[dict] = []
        # UTC timestamp of the most recent successful fetch, or None.
        self.last_updated: datetime | None = None

    async def fetch(self) -> list[dict]:
        """Fetch and parse live vehicle positions from the SCC SIRI feed.

        Returns a list of vehicle dicts (same schema as the original endpoint).
        Errors for individual operators are logged and skipped so a partial
        failure does not prevent the remaining operators from being returned.
        """
        vehicles: list[dict] = []

        async with httpx.AsyncClient(verify=False, timeout=10) as client:  # noqa: S501 – university SIRI endpoint uses a non-standard cert
            for noc in _OPERATORS:
                url = f"https://transport.scc.lancs.ac.uk/bus/live/{noc}"
                try:
                    response = await client.get(url)
                    if response.status_code != 200:
                        continue

                    root = etree.fromstring(response.content)
                    activities = root.xpath(
                        './/siri:VehicleActivity', namespaces=_SIRI_NS
                    )

                    for activity in activities:
                        journey = activity.find(
                            './/siri:MonitoredVehicleJourney', namespaces=_SIRI_NS
                        )
                        if journey is None:
                            continue

                        vehicle_id = journey.findtext(
                            'siri:VehicleRef', namespaces=_SIRI_NS
                        )
                        lat = journey.findtext(
                            './/siri:Latitude', namespaces=_SIRI_NS
                        )
                        lon = journey.findtext(
                            './/siri:Longitude', namespaces=_SIRI_NS
                        )

                        if not (vehicle_id and lat and lon):
                            continue

                        bearing_raw = journey.findtext(
                            'siri:Bearing', namespaces=_SIRI_NS
                        )
                        line_ref = (
                            journey.findtext(
                                'siri:LineRef', namespaces=_SIRI_NS) or ''
                        )
                        line_name = (
                            journey.findtext(
                                'siri:PublishedLineName', namespaces=_SIRI_NS
                            ) or line_ref
                        )
                        direction_ref = (
                            journey.findtext(
                                'siri:DirectionRef', namespaces=_SIRI_NS) or ''
                        )
                        origin_ref = (
                            journey.findtext('siri:OriginRef',
                                             namespaces=_SIRI_NS) or ''
                        )
                        destination_ref = (
                            journey.findtext(
                                'siri:DestinationRef', namespaces=_SIRI_NS) or ''
                        )
                        origin_aimed_departure = (
                            journey.findtext(
                                'siri:OriginAimedDepartureTime', namespaces=_SIRI_NS) or ''
                        )

                        # FramedVehicleJourneyRef contains a journey
                        # identifier that is (almost always) unique per
                        # line+direction within the feed.
                        fvjr_el = journey.find(
                            'siri:FramedVehicleJourneyRef', namespaces=_SIRI_NS)
                        dated_vehicle_journey_ref = ''
                        if fvjr_el is not None:
                            dated_vehicle_journey_ref = (
                                fvjr_el.findtext(
                                    'siri:DatedVehicleJourneyRef',
                                    namespaces=_SIRI_NS) or ''
                            )

                        try:
                            bearing = float(
                                bearing_raw) if bearing_raw else 0.0
                        except ValueError:
                            bearing = 0.0

                        vehicles.append({
                            'vehicle_id': vehicle_id,
                            'operator': noc,
                            'line_ref': line_ref,
                            'line_name': line_name,
                            'latitude': float(lat),
                            'longitude': float(lon),
                            'bearing': bearing,
                            'direction_ref': direction_ref,
                            'origin_ref': origin_ref,
                            'destination_ref': destination_ref,
                            'origin_aimed_departure': origin_aimed_departure,
                            'dated_vehicle_journey_ref': dated_vehicle_journey_ref,
                        })

                except (httpx.HTTPError, etree.XMLSyntaxError) as exc:
                    logger.warning(
                        "Failed to fetch/parse SIRI feed for operator %s: %s", noc, exc
                    )
                except Exception as exc:  # pragma: no cover
                    logger.error(
                        "Unexpected error fetching SIRI feed for operator %s: %s",
                        noc,
                        exc,
                    )

        return vehicles

    async def refresh(self) -> None:
        """Perform a single fetch and update the in-memory cache."""
        vehicles = await self.fetch()
        self.vehicles = vehicles
        self.last_updated = datetime.now(timezone.utc)
        logger.debug("Vehicle cache refreshed: %d vehicles", len(vehicles))

    async def refresh_loop(self, interval_secs: int = REFRESH_INTERVAL_SECS) -> None:
        """Background task: refresh the cache every *interval_secs* seconds.

        Designed to be started with ``asyncio.create_task()`` at application
        startup so it runs for the lifetime of the process.  Handles
        ``asyncio.CancelledError`` cleanly so the task exits without noise when
        the application shuts down.
        """
        try:
            while True:
                try:
                    await self.refresh()
                except Exception as exc:  # pragma: no cover
                    logger.error("Vehicle cache refresh failed: %s", exc)
                await asyncio.sleep(interval_secs)
        except asyncio.CancelledError:
            logger.debug(
                "Vehicle cache refresh loop cancelled — shutting down")


# Singleton shared across the application.
vehicle_cache = VehicleCache()
