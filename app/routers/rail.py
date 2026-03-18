"""
Rail router — endpoints for live rail departure boards, station facilities,
and TRUST-derived train movement data.

These endpoints consume the National Rail Darwin feed (departure boards) and
the station facilities JSON provided by the university transport proxy at
``https://transport.scc.lancs.ac.uk/rail/``.

Regional CRS codes of primary interest (from the PDF):
  BPN, LAY, PFY, BPS, BPB, SQU, SAS, AFV, LTM, MOS, KKM, SLW,
  PRE, LAN, CNF, SVR, BAR, MCM, HHB, LEY, BMB
"""

from fastapi import APIRouter, HTTPException, Query
from app.services.rail_feed import rail_feed

router = APIRouter()

# CRS codes covering the project's core area (Preston–Lancaster–Blackpool
# corridor) as listed in the Transport Application Overview PDF.
REGIONAL_CRS_CODES = [
    "BPN", "LAY", "PFY", "BPS", "BPB", "SQU", "SAS", "AFV",
    "LTM", "MOS", "KKM", "SLW", "PRE", "LAN", "CNF", "SVR",
    "BAR", "MCM", "HHB", "LEY", "BMB",
]


# ------------------------------------------------------------------
# Departures
# ------------------------------------------------------------------

@router.get("/departures/{crs}")
async def get_departures(crs: str):
    """Return the live departure board for a station.

    ``crs`` is the 3-letter CRS code, e.g. ``LAN`` for Lancaster,
    ``PRE`` for Preston.

    The response includes:
    - ``train_services`` — list of upcoming train departures with scheduled
      time, estimated time, platform, operator, delay minutes, calling
      points, and cancellation/delay reason text.
    - ``bus_services`` — rail-replacement bus services.
    - ``messages`` — station-wide alert messages.
    """
    crs = crs.strip().upper()
    if len(crs) != 3 or not crs.isalpha():
        raise HTTPException(
            status_code=400, detail="CRS code must be exactly 3 letters")

    board = await rail_feed.get_departures(crs)

    if board.get("error"):
        raise HTTPException(
            status_code=502,
            detail=f"Unable to fetch departure data for {crs}: {board['error']}",
        )

    return board


# ------------------------------------------------------------------
# Station facilities / information
# ------------------------------------------------------------------

@router.get("/station/{crs}")
async def get_station(crs: str):
    """Return station facility information for a station.

    Includes address, location, operator, accessibility, alerts, and
    available transport links (bus, taxi, etc.).
    """
    crs = crs.strip().upper()
    if len(crs) != 3 or not crs.isalpha():
        raise HTTPException(
            status_code=400, detail="CRS code must be exactly 3 letters")

    info = await rail_feed.get_station_info(crs)

    if info.get("error"):
        raise HTTPException(
            status_code=502,
            detail=f"Unable to fetch station data for {crs}: {info['error']}",
        )

    return info


# ------------------------------------------------------------------
# Regional summary — all regional stations at once
# ------------------------------------------------------------------

@router.get("/regional/summary")
async def regional_summary():
    """Return a compact delay summary for all regional rail stations.

    This fetches the departure board for each station in the project's
    core area and returns a list of ``{crs, station_name, services_count,
    delayed_count, cancelled_count, max_delay_mins, messages}`` entries.

    Useful for painting a region-wide "rail health" overview on a map or
    dashboard without the client having to call each station individually.
    """
    import asyncio

    async def _fetch_one(crs: str):
        board = await rail_feed.get_departures(crs)
        trains = board.get("train_services") or []
        delayed = [s for s in trains if (
            s.get("departure_delay_minutes") or 0) > 0]
        cancelled = [s for s in trains if s.get("is_cancelled")]
        max_delay = max(
            (s.get("departure_delay_minutes") or 0 for s in trains),
            default=0,
        )
        return {
            "crs": crs,
            "station_name": board.get("station_name", crs),
            "services_count": len(trains),
            "delayed_count": len(delayed),
            "cancelled_count": len(cancelled),
            "max_delay_mins": max_delay,
            "messages": board.get("messages", []),
        }

    results = await asyncio.gather(*[_fetch_one(c) for c in REGIONAL_CRS_CODES])
    return {"stations": list(results)}


# ------------------------------------------------------------------
# TRUST live movements (from the background listener)
# ------------------------------------------------------------------

@router.get("/trust/recent")
async def trust_recent_movements(
    stanox: str | None = None,
    train_id: str | None = None,
    limit: int = Query(default=50, le=200),
):
    """Return recent TRUST train movement events from the background listener.

    Optionally filter by ``stanox`` (location code) or ``train_id``.
    """
    from app.services.trust_listener import trust_listener

    events = trust_listener.recent_movements(
        stanox=stanox, train_id=train_id, limit=limit
    )
    return {
        "count": len(events),
        "connected": trust_listener.connected,
        "last_message_at": (
            trust_listener.last_message_at.isoformat()
            if trust_listener.last_message_at
            else None
        ),
        "events": events,
    }


@router.get("/trust/cancellations")
async def trust_cancellations(
    limit: int = Query(default=50, le=200),
):
    """Return recent train cancellation events from the TRUST feed."""
    from app.services.trust_listener import trust_listener

    events = trust_listener.recent_cancellations(limit=limit)
    return {
        "count": len(events),
        "connected": trust_listener.connected,
        "events": events,
    }


@router.get("/trust/activations")
async def trust_activations(
    limit: int = Query(default=50, le=200),
):
    """Return recent train activation events from the TRUST feed."""
    from app.services.trust_listener import trust_listener

    events = trust_listener.recent_activations(limit=limit)
    return {
        "count": len(events),
        "connected": trust_listener.connected,
        "events": events,
    }
