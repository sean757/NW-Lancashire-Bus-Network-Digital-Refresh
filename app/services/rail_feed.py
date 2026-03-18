"""
Rail feed service — fetches and caches live departure/arrival board data from
the National Rail Darwin feed served by the university proxy at
``https://transport.scc.lancs.ac.uk/rail/departures/{CRS}``.

The Darwin feed returns an XML ``StationBoardWithDetails`` document that
contains:
- scheduled and estimated departure times for each service,
- platform numbers,
- operator name and code,
- service IDs,
- origin and destination stations (with CRS codes),
- subsequent calling points with per-stop estimated times,
- cancellation and delay reason text,
- station alert messages.

This module parses the XML into plain dicts and caches the result per station
with a configurable TTL (default 60 s) so that multiple user requests within
the window don't hammer the upstream server.
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from lxml import etree

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Darwin XML namespace prefixes used in the feed.
# The feed uses a mix of namespaces across different RTTI schema versions.
# ---------------------------------------------------------------------------
_NS = {
    "lt":  "http://thalesgroup.com/RTTI/2012-01-13/ldb/types",
    "lt2": "http://thalesgroup.com/RTTI/2014-02-20/ldb/types",
    "lt3": "http://thalesgroup.com/RTTI/2015-05-14/ldb/types",
    "lt4": "http://thalesgroup.com/RTTI/2015-11-27/ldb/types",
    "lt5": "http://thalesgroup.com/RTTI/2016-02-16/ldb/types",
    "lt6": "http://thalesgroup.com/RTTI/2017-02-02/ldb/types",
    "lt7": "http://thalesgroup.com/RTTI/2017-10-01/ldb/types",
    "lt8": "http://thalesgroup.com/RTTI/2021-11-01/ldb/types",
}

_RAIL_BASE = "https://transport.scc.lancs.ac.uk/rail"

# Cache TTL in seconds.  Darwin data rarely changes more often than once per
# minute, and the upstream server has rate-limiting, so 60 s is a safe
# default that keeps our requests well within limits.
_CACHE_TTL_SECS = 60


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text(el: Optional[etree._Element], xpath: str) -> str:
    """Return text of the first element matching *xpath*, or ``""``."""
    if el is None:
        return ""
    found = el.find(xpath, namespaces=_NS)
    return (found.text or "").strip() if found is not None else ""


def _parse_delay_minutes(std: str, etd: str) -> Optional[int]:
    """Compute delay in minutes from scheduled (std) and estimated (etd).

    Returns ``0`` for "On time", ``None`` for "Delayed"/"Cancelled"/blanks,
    or the numeric difference for concrete estimated times like "23:30".
    """
    if not etd or not std:
        return None
    etd_lower = etd.strip().lower()
    if etd_lower == "on time":
        return 0
    if etd_lower in ("delayed", "cancelled", ""):
        return None
    # Try to parse both as HH:MM
    m_std = re.match(r"(\d{1,2}):(\d{2})", std.strip())
    m_etd = re.match(r"(\d{1,2}):(\d{2})", etd.strip())
    if not m_std or not m_etd:
        return None
    std_mins = int(m_std.group(1)) * 60 + int(m_std.group(2))
    etd_mins = int(m_etd.group(1)) * 60 + int(m_etd.group(2))
    diff = etd_mins - std_mins
    # Handle midnight wrap-around (e.g. scheduled 23:50, estimated 00:05)
    if diff < -720:
        diff += 1440
    elif diff > 720:
        diff -= 1440
    return diff


def _parse_calling_point(cp: etree._Element) -> Dict[str, Any]:
    """Parse a single ``<callingPoint>`` element."""
    st = _text(cp, "lt8:st") or _text(cp, "lt4:st")
    et = _text(cp, "lt8:et") or _text(cp, "lt4:et")
    return {
        "station_name": _text(cp, "lt8:locationName") or _text(cp, "lt4:locationName"),
        "crs": _text(cp, "lt8:crs") or _text(cp, "lt4:crs"),
        "scheduled_time": st,
        "estimated_time": et,
        "delay_minutes": _parse_delay_minutes(st, et),
        "length": _text(cp, "lt8:length") or _text(cp, "lt4:length") or None,
    }


def _parse_service(svc: etree._Element) -> Dict[str, Any]:
    """Parse a single ``<service>`` element into a dict."""
    std = _text(svc, "lt4:std")
    etd = _text(svc, "lt4:etd")
    sta = _text(svc, "lt4:sta")
    eta = _text(svc, "lt4:eta")

    # Origin location(s)
    origins = []
    for loc in svc.findall(".//lt5:origin//lt4:location", namespaces=_NS):
        origins.append({
            "name": _text(loc, "lt4:locationName"),
            "crs": _text(loc, "lt4:crs"),
        })

    # Destination location(s)
    destinations = []
    for loc in svc.findall(".//lt5:destination//lt4:location", namespaces=_NS):
        destinations.append({
            "name": _text(loc, "lt4:locationName"),
            "crs": _text(loc, "lt4:crs"),
        })

    # Subsequent calling points
    calling_points: List[Dict[str, Any]] = []
    for cp in svc.findall(".//lt8:callingPoint", namespaces=_NS):
        calling_points.append(_parse_calling_point(cp))

    # Previous calling points (for arrival boards)
    previous_calling_points: List[Dict[str, Any]] = []
    for pcp_list in svc.findall(".//lt8:previousCallingPoints//lt8:callingPointList", namespaces=_NS):
        for cp in pcp_list.findall("lt8:callingPoint", namespaces=_NS):
            previous_calling_points.append(_parse_calling_point(cp))

    # Reason text
    cancel_reason = _text(svc, "lt8:cancelReason") or _text(
        svc, "lt4:cancelReason")
    delay_reason = _text(svc, "lt8:delayReason") or _text(
        svc, "lt4:delayReason")

    platform = _text(svc, "lt4:platform")

    return {
        "service_id": _text(svc, "lt4:serviceID"),
        "service_type": _text(svc, "lt4:serviceType"),
        "operator": _text(svc, "lt4:operator"),
        "operator_code": _text(svc, "lt4:operatorCode"),
        "platform": platform or None,
        "scheduled_departure": std or None,
        "estimated_departure": etd or None,
        "departure_delay_minutes": _parse_delay_minutes(std, etd),
        "scheduled_arrival": sta or None,
        "estimated_arrival": eta or None,
        "arrival_delay_minutes": _parse_delay_minutes(sta, eta),
        "is_cancelled": (etd or "").strip().lower() == "cancelled"
        or (eta or "").strip().lower() == "cancelled",
        "origins": origins,
        "destinations": destinations,
        "calling_points": calling_points,
        "previous_calling_points": previous_calling_points,
        "cancel_reason": cancel_reason or None,
        "delay_reason": delay_reason or None,
        "length": _text(svc, "lt4:length") or None,
    }


# ---------------------------------------------------------------------------
# Feed fetching and caching
# ---------------------------------------------------------------------------

class RailFeedCache:
    """In-memory cache of parsed Darwin departure board data per station."""

    def __init__(self, ttl_secs: int = _CACHE_TTL_SECS):
        self._ttl = ttl_secs
        # CRS -> (fetched_at_utc, parsed_result)
        self._cache: Dict[str, tuple[datetime, Dict[str, Any]]] = {}

    async def get_departures(self, crs: str) -> Dict[str, Any]:
        """Return parsed departure board for *crs* (3-letter station code).

        Results are cached for ``_ttl`` seconds.  The returned dict has the
        structure::

            {
              "station_name": "Lancaster",
              "crs": "LAN",
              "generated_at": "2026-03-18T23:02:49...",
              "messages": ["..."],
              "train_services": [...],
              "bus_services": [...],
              "ferry_services": [...],
            }
        """
        crs = crs.strip().upper()
        now = datetime.now(timezone.utc)

        cached = self._cache.get(crs)
        if cached is not None:
            fetched_at, result = cached
            if (now - fetched_at).total_seconds() < self._ttl:
                return result

        result = await self._fetch_and_parse(crs)
        self._cache[crs] = (now, result)
        return result

    async def get_station_info(self, crs: str) -> Dict[str, Any]:
        """Return station facility information for *crs*.

        This calls the ``/rail/facilities/{CRS}`` JSON endpoint.
        """
        crs = crs.strip().upper()
        cache_key = f"_facilities_{crs}"
        now = datetime.now(timezone.utc)

        cached = self._cache.get(cache_key)
        if cached is not None:
            fetched_at, result = cached
            # Facilities data changes very rarely — cache for 10 minutes.
            if (now - fetched_at).total_seconds() < 600:
                return result

        result = await self._fetch_facilities(crs)
        self._cache[cache_key] = (now, result)
        return result

    # ----- internal ---------------------------------------------------------

    async def _fetch_and_parse(self, crs: str) -> Dict[str, Any]:
        """Fetch the Darwin XML feed and parse it into a dict."""
        url = f"{_RAIL_BASE}/departures/{crs}"
        try:
            async with httpx.AsyncClient(verify=False, timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            logger.warning("Failed to fetch Darwin feed for %s: %s", crs, exc)
            return self._empty_board(crs, error=str(exc))

        try:
            root = etree.fromstring(resp.content)
        except etree.XMLSyntaxError as exc:
            logger.warning("Invalid XML from Darwin for %s: %s", crs, exc)
            return self._empty_board(crs, error="Invalid XML from upstream")

        station_name = _text(root, "lt4:locationName")
        generated_at = _text(root, "lt4:generatedAt")

        # Station alert messages
        messages: List[str] = []
        for msg_el in root.findall(".//lt:message", namespaces=_NS):
            raw = msg_el.text or ""
            # Strip HTML tags for a clean text version
            clean = re.sub(r"<[^>]+>", " ", raw).strip()
            clean = re.sub(r"\s+", " ", clean)
            if clean:
                messages.append(clean)

        # Train services
        train_services = [
            _parse_service(s)
            for s in root.findall(f".//lt8:trainServices/lt8:service", namespaces=_NS)
        ]

        # Bus replacement services
        bus_services = [
            _parse_service(s)
            for s in root.findall(f".//lt8:busServices/lt8:service", namespaces=_NS)
        ]

        # Ferry services (rare but present in schema)
        ferry_services = [
            _parse_service(s)
            for s in root.findall(f".//lt8:ferryServices/lt8:service", namespaces=_NS)
        ]

        return {
            "station_name": station_name or crs,
            "crs": crs,
            "generated_at": generated_at,
            "messages": messages,
            "train_services": train_services,
            "bus_services": bus_services,
            "ferry_services": ferry_services,
        }

    async def _fetch_facilities(self, crs: str) -> Dict[str, Any]:
        """Fetch the station facilities JSON endpoint."""
        url = f"{_RAIL_BASE}/facilities/{crs}"
        try:
            async with httpx.AsyncClient(verify=False, timeout=10) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            logger.warning("Failed to fetch facilities for %s: %s", crs, exc)
            return {"crs": crs, "error": str(exc)}
        except Exception as exc:
            logger.warning("Failed to parse facilities for %s: %s", crs, exc)
            return {"crs": crs, "error": str(exc)}

        # Return a curated subset — the raw JSON can be very large.
        return {
            "crs": crs,
            "name": data.get("name"),
            "address": data.get("address"),
            "location": data.get("location"),
            "station_operator": data.get("stationOperator"),
            "staffing_level": data.get("staffingLevel"),
            "station_alerts": data.get("stationAlerts", []),
            "accessibility": {
                "step_free": (data.get("stationAccessibility") or {})
                .get("stepFreeCategory"),
                "ticket_barriers": (data.get("stationAccessibility") or {})
                .get("ticketBarriers"),
            },
            "toilets_available": bool(
                (data.get("toiletsAndChanging") or {}).get(
                    "toilets", {}).get("available")
            ),
            "transport_links": {
                k: v for k, v in (data.get("transportLinks") or {}).items()
                if v and v.get("available")
            },
            "platforms": data.get("platforms"),
        }

    @staticmethod
    def _empty_board(crs: str, error: str = "") -> Dict[str, Any]:
        return {
            "station_name": crs,
            "crs": crs,
            "generated_at": None,
            "messages": [],
            "train_services": [],
            "bus_services": [],
            "ferry_services": [],
            "error": error or None,
        }


# Module-level singleton — imported by routers and delay_estimator.
rail_feed = RailFeedCache()
