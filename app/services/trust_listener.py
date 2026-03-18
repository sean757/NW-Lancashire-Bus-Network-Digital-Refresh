"""
TRUST listener service — background STOMP consumer for live Network Rail train
movement, activation, and cancellation events.

Connects to ``transport.scc.lancs.ac.uk:61613`` via STOMP and subscribes to:
  - ``/topic/TRAIN_MVT_ALL_TOC`` — train movement feed

Each message batch is a JSON array of event dicts.  Events are stored in a
bounded in-memory ring buffer so the most recent N events are always available
for API queries, without requiring a database.

The STOMP connection runs in a background thread (stomp.py is synchronous) and
is started from the FastAPI ``startup`` event.  If the connection drops, the
listener attempts to reconnect automatically every 30 seconds.

Reference IDs
=============
- **STANOX** — Station Number code used in TRUST.  Mapped to CRS via the
  ``/rail/corpus`` endpoint.
- **train_id** — 10-character unique identity assigned at activation.
- **toc_id** — Operating company code (see Appendix F in PDF).
"""

import json
import logging
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

try:
    import stomp
    _STOMP_AVAILABLE = True
except ImportError:
    _STOMP_AVAILABLE = False

import httpx

logger = logging.getLogger(__name__)

_STOMP_HOST = "transport.scc.lancs.ac.uk"
_STOMP_PORT = 61613
_STOMP_VHOST = "/"
_STOMP_USER = "guest"
_STOMP_PASS = "guest"

# How many events to keep in each ring buffer.
_MAX_MOVEMENTS = 2000
_MAX_CANCELLATIONS = 500
_MAX_ACTIVATIONS = 500

# Time between reconnection attempts (seconds).
_RECONNECT_INTERVAL = 30

# STANOX codes for regional stations of primary interest.
# Populated lazily from the corpus endpoint.
_REGIONAL_STANOX: set[str] = set()

# Corpus endpoint for STANOX ↔ CRS mapping.
_CORPUS_URL = "https://transport.scc.lancs.ac.uk/rail/corpus"


def _load_stanox_crs_map() -> Dict[str, str]:
    """Load the STANOX → CRS mapping from the corpus endpoint.

    Returns a dict mapping STANOX codes to 3-letter CRS codes.
    Only entries that have both a STANOX and a CRS are included.
    """
    try:
        resp = httpx.get(_CORPUS_URL, verify=False,
                         timeout=30, follow_redirects=True)
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to fetch corpus for STANOX mapping: %s", exc)
        return {}

    import gzip
    try:
        raw = gzip.decompress(resp.content)
        data = json.loads(raw)
    except Exception:
        try:
            data = resp.json()
        except Exception as exc:
            logger.warning("Failed to parse corpus JSON: %s", exc)
            return {}

    mapping: Dict[str, str] = {}
    entries = data.get("TIPLOCDATA", data) if isinstance(data, dict) else data
    if isinstance(entries, list):
        for entry in entries:
            stanox = (entry.get("STANOX") or "").strip()
            crs = (entry.get("3ALPHA") or "").strip()
            if stanox and crs and len(crs) == 3:
                mapping[stanox] = crs.upper()
    elif isinstance(entries, dict):
        for key, entry in entries.items():
            if isinstance(entry, dict):
                stanox = (entry.get("STANOX") or "").strip()
                crs = (entry.get("3ALPHA") or "").strip()
                if stanox and crs and len(crs) == 3:
                    mapping[stanox] = crs.upper()
    return mapping


def _timestamp_to_iso(ts_ms: Optional[str]) -> Optional[str]:
    """Convert a TRUST millisecond-epoch timestamp to ISO-8601 string."""
    if not ts_ms:
        return None
    try:
        ms = int(ts_ms)
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return None


class TrustListener:
    """Background STOMP listener for TRUST train movement events."""

    def __init__(self):
        self._movements: Deque[Dict[str, Any]] = deque(maxlen=_MAX_MOVEMENTS)
        self._cancellations: Deque[Dict[str, Any]
                                   ] = deque(maxlen=_MAX_CANCELLATIONS)
        self._activations: Deque[Dict[str, Any]
                                 ] = deque(maxlen=_MAX_ACTIVATIONS)
        self.connected: bool = False
        self.last_message_at: Optional[datetime] = None
        self._stanox_crs: Dict[str, str] = {}
        self._conn: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    # ----- public API (called from async FastAPI handlers) -----------------

    def recent_movements(
        self,
        stanox: Optional[str] = None,
        train_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """Return the most recent movement events, optionally filtered."""
        events = list(self._movements)
        events.reverse()  # newest first
        if stanox:
            events = [e for e in events if e.get("loc_stanox") == stanox]
        if train_id:
            events = [e for e in events if e.get("train_id") == train_id]
        return events[:limit]

    def recent_cancellations(self, limit: int = 50) -> List[Dict[str, Any]]:
        events = list(self._cancellations)
        events.reverse()
        return events[:limit]

    def recent_activations(self, limit: int = 50) -> List[Dict[str, Any]]:
        events = list(self._activations)
        events.reverse()
        return events[:limit]

    def get_stanox_crs(self, stanox: str) -> Optional[str]:
        """Resolve a STANOX to a CRS code, or None."""
        return self._stanox_crs.get(stanox)

    # ----- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start the STOMP listener in a background daemon thread."""
        if not _STOMP_AVAILABLE:
            logger.warning(
                "stomp.py not installed — TRUST listener disabled. "
                "Install with: pip install stomp.py"
            )
            return

        # Load corpus mapping synchronously (runs once at startup).
        self._stanox_crs = _load_stanox_crs_map()
        logger.info(
            "Loaded %d STANOX→CRS mappings for TRUST listener",
            len(self._stanox_crs),
        )

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="trust-listener"
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the background thread to stop."""
        self._stop_event.set()
        if self._conn:
            try:
                self._conn.disconnect()
            except Exception:
                pass

    # ----- internal --------------------------------------------------------

    def _run_loop(self) -> None:
        """Main loop that connects and reconnects."""
        while not self._stop_event.is_set():
            try:
                self._connect()
                # Block until the stop event is set or connection drops.
                while not self._stop_event.is_set() and self.connected:
                    self._stop_event.wait(timeout=5)
            except Exception as exc:
                logger.warning("TRUST listener error: %s", exc)
                self.connected = False
            # Wait before reconnecting.
            if not self._stop_event.is_set():
                logger.info(
                    "TRUST listener reconnecting in %d s…", _RECONNECT_INTERVAL
                )
                self._stop_event.wait(timeout=_RECONNECT_INTERVAL)

    def _connect(self) -> None:
        """Establish STOMP connection and subscribe."""
        listener = _StompCallback(self)
        conn = stomp.Connection(
            [(_STOMP_HOST, _STOMP_PORT)],
            vhost=_STOMP_VHOST,
        )
        conn.set_listener("trust", listener)
        conn.connect(username=_STOMP_USER, passcode=_STOMP_PASS, wait=True)
        conn.subscribe(
            destination="/topic/TRAIN_MVT_ALL_TOC", id="1", ack="auto"
        )
        self._conn = conn
        self.connected = True
        logger.info("TRUST STOMP listener connected")

    def _on_message(self, body: str) -> None:
        """Process a single STOMP message body (JSON array of events)."""
        self.last_message_at = datetime.now(timezone.utc)
        try:
            batch = json.loads(body)
        except json.JSONDecodeError:
            return

        if not isinstance(batch, list):
            batch = [batch]

        for event in batch:
            header = event.get("header", {})
            msg_body = event.get("body", {})
            msg_type = header.get("msg_type", "")

            if msg_type == "0003":  # Train Movement
                self._handle_movement(msg_body)
            elif msg_type == "0001":  # Train Activation
                self._handle_activation(msg_body)
            elif msg_type == "0002":  # Train Cancellation
                self._handle_cancellation(msg_body)
            # 0005-0008 are less frequent; ignore for now.

    def _handle_movement(self, body: Dict) -> None:
        stanox = (body.get("loc_stanox") or "").strip()
        crs = self._stanox_crs.get(stanox)
        event = {
            "type": "movement",
            "train_id": body.get("train_id"),
            "event_type": body.get("event_type"),  # ARRIVAL / DEPARTURE
            "loc_stanox": stanox,
            "crs": crs,
            "platform": (body.get("platform") or "").strip() or None,
            "planned_time": _timestamp_to_iso(body.get("planned_timestamp")),
            "actual_time": _timestamp_to_iso(body.get("actual_timestamp")),
            "timetable_variation": int(body.get("timetable_variation") or 0),
            "variation_status": body.get("variation_status"),
            "toc_id": body.get("toc_id"),
            "train_terminated": body.get("train_terminated") == "true",
            "direction": body.get("direction_ind"),
            "delay_monitoring_point": body.get("delay_monitoring_point") == "true",
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        self._movements.append(event)

    def _handle_activation(self, body: Dict) -> None:
        origin_stanox = (
            body.get("tp_origin_stanox")
            or body.get("sched_origin_stanox")
            or ""
        ).strip()
        event = {
            "type": "activation",
            "train_id": body.get("train_id"),
            "train_uid": body.get("train_uid"),
            "toc_id": body.get("toc_id"),
            "origin_stanox": origin_stanox,
            "origin_crs": self._stanox_crs.get(origin_stanox),
            "schedule_type": body.get("schedule_type"),
            "origin_departure": _timestamp_to_iso(
                body.get("origin_dep_timestamp")
            ),
            "schedule_wtt_id": body.get("schedule_wtt_id"),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        self._activations.append(event)

    def _handle_cancellation(self, body: Dict) -> None:
        stanox = (body.get("loc_stanox") or "").strip()
        event = {
            "type": "cancellation",
            "train_id": body.get("train_id"),
            "toc_id": body.get("toc_id"),
            "loc_stanox": stanox,
            "crs": self._stanox_crs.get(stanox),
            "cancel_type": body.get("canx_type"),
            "cancel_reason_code": body.get("canx_reason_code"),
            "cancel_time": _timestamp_to_iso(body.get("canx_timestamp")),
            "departure_time": _timestamp_to_iso(body.get("dep_timestamp")),
            "received_at": datetime.now(timezone.utc).isoformat(),
        }
        self._cancellations.append(event)

    def _on_disconnected(self) -> None:
        self.connected = False
        logger.warning("TRUST STOMP listener disconnected")


class _StompCallback(stomp.ConnectionListener):
    """Adapter that forwards stomp.py callbacks to the TrustListener."""

    def __init__(self, parent: TrustListener):
        self._parent = parent

    def on_message(self, frame):
        try:
            self._parent._on_message(frame.body)
        except Exception as exc:
            logger.warning("Error processing TRUST message: %s", exc)

    def on_error(self, frame):
        logger.warning("STOMP error frame: %s", frame.body)

    def on_disconnected(self):
        self._parent._on_disconnected()


# Module-level singleton.
trust_listener = TrustListener()
