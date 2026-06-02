"""
pipeline/zones.py
Zone classification — entry/exit detection, floor zone mapping,
billing-area detection, dwell tracking, and queue-depth counting.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("pipeline.zones")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ZoneDef:
    zone_id: str
    zone_name: str
    camera_ids: List[str]


@dataclass
class CameraDef:
    camera_id: str
    file: str
    coverage: str  # "entry_exit" | "main_floor" | "billing"
    description: str
    entry_line_y_frac: Optional[float] = None
    direction: Optional[str] = None
    zones_in_view: Optional[List[str]] = None
    billing_zone_bbox_frac: Optional[List[float]] = None
    queue_region_bbox_frac: Optional[List[float]] = None


@dataclass
class _TrackZoneState:
    """Per-track zone tracking state for a single camera."""
    current_zone: Optional[str] = None
    zone_enter_time: float = 0.0          # seconds since video start
    last_dwell_emit: float = 0.0          # last dwell event time
    prev_y_frac: Optional[float] = None   # for entry/exit vertical tracking
    crossed_entry_line: bool = False
    in_billing_zone: bool = False
    in_queue_region: bool = False
    queue_join_time: float = 0.0


# ---------------------------------------------------------------------------
# ZoneClassifier
# ---------------------------------------------------------------------------

class ZoneClassifier:
    """
    Classifies which zone a detected person belongs to, given their bounding
    box centroid and the camera they are observed on.
    """

    DWELL_EMIT_INTERVAL_S: float = 30.0  # emit ZONE_DWELL every 30 s

    def __init__(self, store_layout_path: str | Path) -> None:
        layout = json.loads(Path(store_layout_path).read_text(encoding="utf-8"))
        store = layout["stores"][0]  # single-store for now

        self.store_id: str = store["store_id"]
        self.cameras: Dict[str, CameraDef] = {}
        self.zones: Dict[str, ZoneDef] = {}
        self._file_to_camera: Dict[str, str] = {}

        for z in store.get("zones", []):
            self.zones[z["zone_id"]] = ZoneDef(**z)

        for c in store.get("cameras", []):
            cam = CameraDef(
                camera_id=c["camera_id"],
                file=c.get("file", ""),
                coverage=c.get("coverage", ""),
                description=c.get("description", ""),
                entry_line_y_frac=c.get("entry_line_y_frac"),
                direction=c.get("direction"),
                zones_in_view=c.get("zones_in_view"),
                billing_zone_bbox_frac=c.get("billing_zone_bbox_frac"),
                queue_region_bbox_frac=c.get("queue_region_bbox_frac"),
            )
            self.cameras[cam.camera_id] = cam
            self._file_to_camera[cam.file] = cam.camera_id

        # per-camera, per-track zone state
        self._states: Dict[str, Dict[int, _TrackZoneState]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def camera_id_for_file(self, filename: str) -> Optional[str]:
        """Map a video filename (e.g. 'CAM 1.mp4') → camera_id."""
        return self._file_to_camera.get(filename)

    def classify(
        self,
        camera_id: str,
        track_id: int,
        bbox: Tuple[float, float, float, float],
        frame_w: int,
        frame_h: int,
        current_time_s: float,
    ) -> List[Dict[str, Any]]:
        """
        Given a detection on *camera_id* with bounding box ``(x1, y1, x2, y2)``
        in pixel coords, return a list of zone events to emit.

        Each returned dict has keys:
            event_type, zone_id, dwell_ms (if applicable)
        """
        cam = self.cameras.get(camera_id)
        if cam is None:
            return []

        if camera_id not in self._states:
            self._states[camera_id] = {}
        if track_id not in self._states[camera_id]:
            self._states[camera_id][track_id] = _TrackZoneState()

        state = self._states[camera_id][track_id]
        events: List[Dict[str, Any]] = []

        x1, y1, x2, y2 = bbox
        cx = (x1 + x2) / 2.0
        cy = (y1 + y2) / 2.0
        cx_frac = cx / max(frame_w, 1)
        cy_frac = cy / max(frame_h, 1)

        if cam.coverage == "entry_exit":
            events.extend(self._classify_entry_exit(cam, state, cy_frac, current_time_s))
        elif cam.coverage == "main_floor":
            events.extend(self._classify_floor(cam, state, cx_frac, cy_frac, current_time_s))
        elif cam.coverage == "billing":
            events.extend(self._classify_billing(cam, state, cx_frac, cy_frac, current_time_s))

        return events

    def get_queue_depth(self, camera_id: str) -> int:
        """Return the current number of tracked people in the queue region."""
        states = self._states.get(camera_id, {})
        return sum(1 for s in states.values() if s.in_queue_region)

    def track_lost(
        self,
        camera_id: str,
        track_id: int,
        current_time_s: float,
    ) -> List[Dict[str, Any]]:
        """
        Called when a track disappears.  Emits ZONE_EXIT / queue-abandon
        events if the person was still inside a zone.
        """
        states = self._states.get(camera_id, {})
        state = states.pop(track_id, None)
        if state is None:
            return []

        events: List[Dict[str, Any]] = []
        cam = self.cameras.get(camera_id)

        if state.current_zone and cam and cam.coverage == "main_floor":
            dwell_ms = int((current_time_s - state.zone_enter_time) * 1000)
            events.append({
                "event_type": "ZONE_EXIT",
                "zone_id": state.current_zone,
                "dwell_ms": max(dwell_ms, 0),
            })

        if state.in_queue_region and cam and cam.coverage == "billing":
            dwell_ms = int((current_time_s - state.queue_join_time) * 1000)
            events.append({
                "event_type": "BILLING_QUEUE_ABANDON",
                "zone_id": "BILLING",
                "dwell_ms": max(dwell_ms, 0),
                "queue_depth": self.get_queue_depth(camera_id),
            })

        return events

    # ------------------------------------------------------------------
    # entry / exit camera
    # ------------------------------------------------------------------

    def _classify_entry_exit(
        self,
        cam: CameraDef,
        state: _TrackZoneState,
        cy_frac: float,
        current_time_s: float,
    ) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        threshold = cam.entry_line_y_frac or 0.55

        if state.prev_y_frac is not None:
            # Moving downward across the line → ENTRY
            if state.prev_y_frac < threshold <= cy_frac:
                events.append({"event_type": "ENTRY", "zone_id": None, "dwell_ms": 0})
            # Moving upward across the line → EXIT
            elif state.prev_y_frac > threshold >= cy_frac:
                events.append({"event_type": "EXIT", "zone_id": None, "dwell_ms": 0})

        state.prev_y_frac = cy_frac
        return events

    # ------------------------------------------------------------------
    # floor cameras — map horizontal position to product zones
    # ------------------------------------------------------------------

    def _classify_floor(
        self,
        cam: CameraDef,
        state: _TrackZoneState,
        cx_frac: float,
        cy_frac: float,
        current_time_s: float,
    ) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        zones_in_view = cam.zones_in_view or []
        if not zones_in_view:
            return events

        # divide frame horizontally into equal-width zones
        n = len(zones_in_view)
        zone_width = 1.0 / n
        idx = min(int(cx_frac / zone_width), n - 1)
        detected_zone = zones_in_view[idx]

        if detected_zone != state.current_zone:
            # emit ZONE_EXIT for old zone
            if state.current_zone is not None:
                dwell_ms = int((current_time_s - state.zone_enter_time) * 1000)
                events.append({
                    "event_type": "ZONE_EXIT",
                    "zone_id": state.current_zone,
                    "dwell_ms": max(dwell_ms, 0),
                })

            # emit ZONE_ENTER for new zone
            events.append({
                "event_type": "ZONE_ENTER",
                "zone_id": detected_zone,
                "dwell_ms": 0,
            })
            state.current_zone = detected_zone
            state.zone_enter_time = current_time_s
            state.last_dwell_emit = current_time_s

        # periodic dwell
        elif state.current_zone is not None:
            elapsed_since_dwell = current_time_s - state.last_dwell_emit
            if elapsed_since_dwell >= self.DWELL_EMIT_INTERVAL_S:
                dwell_ms = int((current_time_s - state.zone_enter_time) * 1000)
                events.append({
                    "event_type": "ZONE_DWELL",
                    "zone_id": state.current_zone,
                    "dwell_ms": max(dwell_ms, 0),
                })
                state.last_dwell_emit = current_time_s

        return events

    # ------------------------------------------------------------------
    # billing camera
    # ------------------------------------------------------------------

    def _classify_billing(
        self,
        cam: CameraDef,
        state: _TrackZoneState,
        cx_frac: float,
        cy_frac: float,
        current_time_s: float,
    ) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []

        # billing zone
        if cam.billing_zone_bbox_frac:
            bx1, by1, bx2, by2 = cam.billing_zone_bbox_frac
            in_billing = bx1 <= cx_frac <= bx2 and by1 <= cy_frac <= by2

            if in_billing and not state.in_billing_zone:
                events.append({
                    "event_type": "ZONE_ENTER",
                    "zone_id": "BILLING",
                    "dwell_ms": 0,
                })
                state.in_billing_zone = True
                state.zone_enter_time = current_time_s
                state.last_dwell_emit = current_time_s
                state.current_zone = "BILLING"
            elif not in_billing and state.in_billing_zone:
                dwell_ms = int((current_time_s - state.zone_enter_time) * 1000)
                events.append({
                    "event_type": "ZONE_EXIT",
                    "zone_id": "BILLING",
                    "dwell_ms": max(dwell_ms, 0),
                })
                state.in_billing_zone = False
                state.current_zone = None
            elif in_billing and state.in_billing_zone:
                # periodic dwell
                elapsed = current_time_s - state.last_dwell_emit
                if elapsed >= self.DWELL_EMIT_INTERVAL_S:
                    dwell_ms = int((current_time_s - state.zone_enter_time) * 1000)
                    events.append({
                        "event_type": "ZONE_DWELL",
                        "zone_id": "BILLING",
                        "dwell_ms": max(dwell_ms, 0),
                    })
                    state.last_dwell_emit = current_time_s

        # queue region
        if cam.queue_region_bbox_frac:
            qx1, qy1, qx2, qy2 = cam.queue_region_bbox_frac
            in_queue = qx1 <= cx_frac <= qx2 and qy1 <= cy_frac <= qy2

            if in_queue and not state.in_queue_region:
                state.in_queue_region = True
                state.queue_join_time = current_time_s
                events.append({
                    "event_type": "BILLING_QUEUE_JOIN",
                    "zone_id": "BILLING",
                    "dwell_ms": 0,
                    "queue_depth": self.get_queue_depth(
                        cam.camera_id,
                    ),
                })
            elif not in_queue and state.in_queue_region:
                dwell_ms = int((current_time_s - state.queue_join_time) * 1000)
                state.in_queue_region = False
                events.append({
                    "event_type": "BILLING_QUEUE_ABANDON",
                    "zone_id": "BILLING",
                    "dwell_ms": max(dwell_ms, 0),
                    "queue_depth": self.get_queue_depth(
                        cam.camera_id,
                    ),
                })

        return events
