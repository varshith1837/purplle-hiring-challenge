"""
pipeline/emit.py
Event emission — writes JSONL lines and optionally POSTs batches to the API.
Every emitted event matches the EventIn schema from app/models.py exactly.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger("pipeline.emit")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    """Return current UTC time in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _uuid4() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# EventEmitter
# ---------------------------------------------------------------------------

class EventEmitter:
    """
    Writes events to a JSONL file and optionally batch-POSTs them to the
    Store Intelligence ingest API.

    Parameters
    ----------
    output_path : Path | str
        Destination ``.jsonl`` file (appended to).
    api_url : str | None
        Base URL of the API, e.g. ``http://localhost:8000``.
        When set, events are batch-POSTed to ``{api_url}/api/ingest``.
    batch_size : int
        How many events to accumulate before flushing to the API.
    flush_interval : float
        Maximum seconds between API flushes (even if the batch is not full).
    """

    def __init__(
        self,
        output_path: Path | str,
        api_url: Optional[str] = None,
        batch_size: int = 50,
        flush_interval: float = 5.0,
    ) -> None:
        self.output_path = Path(output_path)
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        self.api_url = api_url.rstrip("/") if api_url else None
        self.batch_size = batch_size
        self.flush_interval = flush_interval

        self._file = open(self.output_path, "a", encoding="utf-8")
        self._buffer: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._last_flush = time.monotonic()

        # session-seq counters per visitor_id
        self._session_seq: Dict[str, int] = {}

        self._total_emitted = 0

        # background flush timer
        if self.api_url:
            self._timer_running = True
            self._timer = threading.Thread(target=self._flush_loop, daemon=True)
            self._timer.start()
        else:
            self._timer_running = False

    # ------------------------------------------------------------------
    # public helpers — one per event type
    # ------------------------------------------------------------------

    def emit_entry(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        confidence: float,
        is_staff: bool = False,
    ) -> Dict[str, Any]:
        return self._emit(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type="ENTRY",
            timestamp=timestamp,
            confidence=confidence,
            is_staff=is_staff,
        )

    def emit_exit(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        confidence: float,
        dwell_ms: int = 0,
        is_staff: bool = False,
    ) -> Dict[str, Any]:
        return self._emit(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type="EXIT",
            timestamp=timestamp,
            confidence=confidence,
            dwell_ms=dwell_ms,
            is_staff=is_staff,
        )

    def emit_zone_enter(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        zone_id: str,
        confidence: float,
        is_staff: bool = False,
        sku_zone: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._emit(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type="ZONE_ENTER",
            timestamp=timestamp,
            zone_id=zone_id,
            confidence=confidence,
            is_staff=is_staff,
            sku_zone=sku_zone,
        )

    def emit_zone_exit(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        zone_id: str,
        confidence: float,
        dwell_ms: int = 0,
        is_staff: bool = False,
    ) -> Dict[str, Any]:
        return self._emit(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type="ZONE_EXIT",
            timestamp=timestamp,
            zone_id=zone_id,
            confidence=confidence,
            dwell_ms=dwell_ms,
            is_staff=is_staff,
        )

    def emit_zone_dwell(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        zone_id: str,
        dwell_ms: int,
        confidence: float,
        is_staff: bool = False,
    ) -> Dict[str, Any]:
        return self._emit(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type="ZONE_DWELL",
            timestamp=timestamp,
            zone_id=zone_id,
            dwell_ms=dwell_ms,
            confidence=confidence,
            is_staff=is_staff,
        )

    def emit_billing_queue_join(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        confidence: float,
        queue_depth: int = 0,
        is_staff: bool = False,
    ) -> Dict[str, Any]:
        return self._emit(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type="BILLING_QUEUE_JOIN",
            timestamp=timestamp,
            zone_id="BILLING",
            confidence=confidence,
            is_staff=is_staff,
            queue_depth=queue_depth,
        )

    def emit_billing_queue_abandon(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        confidence: float,
        dwell_ms: int = 0,
        queue_depth: int = 0,
        is_staff: bool = False,
    ) -> Dict[str, Any]:
        return self._emit(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type="BILLING_QUEUE_ABANDON",
            timestamp=timestamp,
            zone_id="BILLING",
            confidence=confidence,
            dwell_ms=dwell_ms,
            is_staff=is_staff,
            queue_depth=queue_depth,
        )

    def emit_reentry(
        self,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        timestamp: str,
        confidence: float,
        is_staff: bool = False,
    ) -> Dict[str, Any]:
        return self._emit(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type="REENTRY",
            timestamp=timestamp,
            confidence=confidence,
            is_staff=is_staff,
        )

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _next_session_seq(self, visitor_id: str) -> int:
        seq = self._session_seq.get(visitor_id, 0) + 1
        self._session_seq[visitor_id] = seq
        return seq

    def _emit(
        self,
        *,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        event_type: str,
        timestamp: str,
        confidence: float,
        zone_id: Optional[str] = None,
        dwell_ms: int = 0,
        is_staff: bool = False,
        sku_zone: Optional[str] = None,
        queue_depth: Optional[int] = None,
    ) -> Dict[str, Any]:
        session_seq = self._next_session_seq(visitor_id)

        metadata: Dict[str, Any] = {}
        if queue_depth is not None:
            metadata["queue_depth"] = queue_depth
        if sku_zone is not None:
            metadata["sku_zone"] = sku_zone
        metadata["session_seq"] = session_seq

        event: Dict[str, Any] = {
            "event_id": _uuid4(),
            "store_id": store_id,
            "camera_id": camera_id,
            "visitor_id": visitor_id,
            "event_type": event_type,
            "timestamp": timestamp,
            "zone_id": zone_id,
            "dwell_ms": dwell_ms,
            "is_staff": is_staff,
            "confidence": round(float(confidence), 4),
            "metadata": metadata,
        }

        # write to JSONL
        line = json.dumps(event, default=str)
        self._file.write(line + "\n")
        self._file.flush()

        self._total_emitted += 1

        # buffer for API batch
        if self.api_url:
            with self._lock:
                self._buffer.append(event)
                if len(self._buffer) >= self.batch_size:
                    self._flush_to_api()

        return event

    def _flush_to_api(self) -> None:
        """Send buffered events to the ingest API endpoint."""
        if not self._buffer:
            return
        batch = list(self._buffer)
        self._buffer.clear()
        self._last_flush = time.monotonic()

        url = f"{self.api_url}/events/ingest"
        payload = {"events": batch}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                body = resp.json()
                logger.info(
                    "API batch sent: accepted=%s rejected=%s duplicates=%s",
                    body.get("accepted"),
                    body.get("rejected"),
                    body.get("duplicates"),
                )
            else:
                logger.warning("API returned %s: %s", resp.status_code, resp.text[:200])
        except requests.RequestException as exc:
            logger.error("Failed to POST events: %s", exc)

    def _flush_loop(self) -> None:
        """Background thread that flushes the API buffer periodically."""
        while self._timer_running:
            time.sleep(1.0)
            with self._lock:
                elapsed = time.monotonic() - self._last_flush
                if elapsed >= self.flush_interval and self._buffer:
                    self._flush_to_api()

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Flush remaining events and close the JSONL file handle."""
        self._timer_running = False
        if self.api_url:
            with self._lock:
                self._flush_to_api()
        self._file.close()
        logger.info("EventEmitter closed. Total events emitted: %d", self._total_emitted)

    @property
    def total_emitted(self) -> int:
        return self._total_emitted
