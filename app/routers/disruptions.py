"""
Disruptions router — endpoints for querying and managing disruptions.
Also includes a proxy for live bus positions (CORS workaround).
"""

from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text
from app.database import get_db
import httpx
from lxml import etree
import logging

logger = logging.getLogger(__name__)

router = APIRouter()


# ==========================================
# Disruptions
# ==========================================

@router.get("/")
async def list_disruptions(
    active_only: bool = True,
    route_id: str | None = None,
    severity: str | None = None,
    disruption_type: str | None = None,
    limit: int = Query(default=50, le=200),
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """List disruptions with optional filters."""
    query = "SELECT * FROM disruptions WHERE 1=1"
    params = {}

    if active_only:
        query += " AND active = TRUE"

    if route_id:
        query += " AND route_id = :route_id"
        params["route_id"] = route_id

    if severity:
        query += " AND severity = :severity"
        params["severity"] = severity

    if disruption_type:
        query += " AND disruption_type = :disruption_type"
        params["disruption_type"] = disruption_type

    query += " ORDER BY detected_at DESC LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset

    result = await db.execute(text(query), params)
    rows = result.mappings().all()
    return [dict(row) for row in rows]


@router.get("/active")
async def active_disruptions(db: AsyncSession = Depends(get_db)):
    """Get all currently active disruptions."""
    query = """
        SELECT d.*, r.route_name, s.stop_name
        FROM disruptions d
        LEFT JOIN routes r ON d.route_id = r.route_id
        LEFT JOIN stops s ON d.stop_id = s.stop_id
        WHERE d.active = TRUE
        ORDER BY
            CASE d.severity
                WHEN 'severe' THEN 1
                WHEN 'moderate' THEN 2
                WHEN 'minor' THEN 3
            END,
            d.detected_at DESC
    """
    result = await db.execute(text(query))
    rows = result.mappings().all()
    return [dict(row) for row in rows]


@router.get("/{disruption_id}")
async def get_disruption(disruption_id: int, db: AsyncSession = Depends(get_db)):
    """Get a single disruption by ID."""
    query = """
        SELECT d.*, r.route_name, s.stop_name
        FROM disruptions d
        LEFT JOIN routes r ON d.route_id = r.route_id
        LEFT JOIN stops s ON d.stop_id = s.stop_id
        WHERE d.id = :disruption_id
    """
    result = await db.execute(text(query), {"disruption_id": disruption_id})
    row = result.mappings().first()

    if not row:
        raise HTTPException(status_code=404, detail="Disruption not found")

    return dict(row)


# ==========================================
# Live Bus Positions (proxy for CORS)
# ==========================================
# The university transport API blocks browser requests,
# so we proxy live position data through our backend.

OPERATORS = ["ARCT", "BLAC", "KLCO", "SCCU", "SCMY", "NUTT"]


@router.get("/live/buses")
async def get_live_buses(operator: str | None = None):
    """Proxy live bus positions from the university SIRI feed.
    Optionally filter by operator code (e.g. ARCT, BLAC, SCCU)."""
    operators = [operator] if operator else OPERATORS
    all_vehicles = []

    async with httpx.AsyncClient(verify=False, timeout=10) as client:
        for noc in operators:
            try:
                url = f"https://transport.scc.lancs.ac.uk/bus/live/{noc}"
                response = await client.get(url)
                if response.status_code == 200:
                    all_vehicles.append({
                        "operator": noc,
                        "data": response.text
                    })
            except Exception:
                continue

    return {"operators_queried": operators, "results": all_vehicles}


@router.get("/live/positions")
async def get_live_positions_from_db(
    route_id: str | None = None,
    limit: int = Query(default=100, le=500),
    db: AsyncSession = Depends(get_db),
):
    """Get the latest live positions from the database."""
    query = """
        SELECT DISTINCT ON (vehicle_id)
            vehicle_id, route_id, latitude, longitude, bearing, speed, delay_seconds, source, recorded_at
        FROM live_positions
        WHERE received_at > NOW() - INTERVAL '5 minutes'
    """
    params = {"limit": limit}

    if route_id:
        query += " AND route_id = :route_id"
        params["route_id"] = route_id

    query += " ORDER BY vehicle_id, received_at DESC LIMIT :limit"

    result = await db.execute(text(query), params)
    rows = result.mappings().all()
    return [dict(row) for row in rows]


# SIRI XML namespace
_SIRI_NS = {'siri': 'http://www.siri.org.uk/siri'}


@router.get("/live/vehicles")
async def get_live_vehicles():
    """Fetch and parse live bus positions from the SIRI feed for all operators.

    Returns a list of vehicle dicts with vehicle_id, operator, line_ref,
    line_name, latitude, longitude, and bearing.  This endpoint is the
    recommended source for the frontend map because it returns structured JSON
    without requiring the database ingest script to be running.
    """
    vehicles = []

    async with httpx.AsyncClient(verify=False, timeout=10) as client:  # noqa: S501 – university SIRI endpoint uses a non-standard cert
        for noc in OPERATORS:
            url = f"https://transport.scc.lancs.ac.uk/bus/live/{noc}"
            try:
                response = await client.get(url)
                if response.status_code != 200:
                    continue

                root = etree.fromstring(response.content)
                activities = root.xpath('.//siri:VehicleActivity', namespaces=_SIRI_NS)

                for activity in activities:
                    journey = activity.find('.//siri:MonitoredVehicleJourney', namespaces=_SIRI_NS)
                    if journey is None:
                        continue

                    vehicle_id = journey.findtext('siri:VehicleRef', namespaces=_SIRI_NS)
                    lat = journey.findtext('.//siri:Latitude', namespaces=_SIRI_NS)
                    lon = journey.findtext('.//siri:Longitude', namespaces=_SIRI_NS)

                    if not (vehicle_id and lat and lon):
                        continue

                    bearing_raw = journey.findtext('siri:Bearing', namespaces=_SIRI_NS)
                    line_ref = journey.findtext('siri:LineRef', namespaces=_SIRI_NS) or ''
                    line_name = journey.findtext('siri:PublishedLineName', namespaces=_SIRI_NS) or line_ref

                    try:
                        bearing = float(bearing_raw) if bearing_raw else 0.0
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
                    })

            except (httpx.HTTPError, etree.XMLSyntaxError) as exc:
                logger.warning("Failed to fetch/parse SIRI feed for operator %s: %s", noc, exc)
                continue
            except Exception as exc:  # pragma: no cover
                logger.error("Unexpected error for operator %s: %s", noc, exc)
                continue

    return {'vehicles': vehicles}