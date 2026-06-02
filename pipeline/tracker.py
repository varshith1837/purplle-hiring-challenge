"""
pipeline/tracker.py
Re-ID, visitor-ID assignment, re-entry detection, cross-camera
deduplication, and session tracking.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import cv2
import numpy as np

logger = logging.getLogger("pipeline.tracker")


# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

REENTRY_WINDOW_S: float = 300.0            # 5 minutes
APPEARANCE_SIM_THRESHOLD: float = 0.55     # histogram correlation threshold
CROSS_CAM_TIME_WINDOW_S: float = 30.0      # max seconds gap for cross-cam match


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class TrackState:
    """State for a single tracked person on a single camera."""
    track_id: int
    visitor_id: str
    entry_time: float                      # seconds since video start
    last_seen: float = 0.0
    zones_visited: List[str] = field(default_factory=list)
    current_zone: Optional[str] = None
    is_exited: bool = False
    is_staff: bool = False
    appearance_hist: Optional[np.ndarray] = None


@dataclass
class VisitorRecord:
    """Global record for a unique visitor (may span cameras)."""
    visitor_id: str
    first_seen: float
    last_seen: float
    session_seq: int = 1
    camera_ids: Set[str] = field(default_factory=set)
    appearance_hist: Optional[np.ndarray] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_visitor_id(track_id: int, camera_id: str, t: float) -> str:
    """Generate a short, deterministic visitor token."""
    raw = f"{camera_id}:{track_id}:{t}"
    short_hash = hashlib.sha256(raw.encode()).hexdigest()[:8]
    return f"VIS_{short_hash}"


def compute_color_histogram(
    frame: np.ndarray,
    bbox: Tuple[int, int, int, int],
) -> Optional[np.ndarray]:
    """
    Compute a normalised HSV colour histogram for the bounding-box region.
    Returns None if the region is invalid.
    """
    x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
    h, w = frame.shape[:2]
    x1, y1 = max(0, x1), max(0, y1)
    x2, y2 = min(w, x2), min(h, y2)

    if x2 <= x1 or y2 <= y1:
        return None

    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        return None

    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [30, 32], [0, 180, 0, 256])
    cv2.normalize(hist, hist)
    return hist


def compare_histograms(h1: np.ndarray, h2: np.ndarray) -> float:
    """Return correlation between two histograms (0..1 range)."""
    val = cv2.compareHist(h1, h2, cv2.HISTCMP_CORREL)
    return float(max(0.0, val))


# ---------------------------------------------------------------------------
# TrackerState
# ---------------------------------------------------------------------------

class TrackerState:
    """
    Maintains per-camera track histories, assigns visitor IDs, detects
    re-entries, and performs cross-camera deduplication.
    """

    def __init__(
        self,
        reentry_window_s: float = REENTRY_WINDOW_S,
        appearance_sim_threshold: float = APPEARANCE_SIM_THRESHOLD,
        cross_cam_time_window_s: float = CROSS_CAM_TIME_WINDOW_S,
    ) -> None:
        self.reentry_window_s = reentry_window_s
        self.appearance_sim_threshold = appearance_sim_threshold
        self.cross_cam_time_window_s = cross_cam_time_window_s

        # camera_id → {track_id → TrackState}
        self._tracks: Dict[str, Dict[int, TrackState]] = {}

        # visitor_id → VisitorRecord  (global registry)
        self._visitors: Dict[str, VisitorRecord] = {}

        # camera_id → list of recently-exited TrackStates (for re-entry)
        self._recent_exits: Dict[str, List[TrackState]] = {}

        # session-seq counters
        self._session_seq: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def update(
        self,
        camera_id: str,
        track_id: int,
        frame: np.ndarray,
        bbox: Tuple[int, int, int, int],
        current_time_s: float,
    ) -> Tuple[str, bool]:
        """
        Update state for a detection.

        Returns
        -------
        (visitor_id, is_reentry)
        """
        if camera_id not in self._tracks:
            self._tracks[camera_id] = {}
            self._recent_exits[camera_id] = []

        ts = self._tracks[camera_id].get(track_id)

        if ts is not None:
            # Existing track — update
            ts.last_seen = current_time_s
            # update appearance periodically (every 30 frames ~ 1 s)
            hist = compute_color_histogram(frame, bbox)
            if hist is not None:
                ts.appearance_hist = hist
            return ts.visitor_id, False

        # Check if ByteTrack just recovered a recently lost track ID
        for i, ex in enumerate(self._recent_exits.get(camera_id, [])):
            if ex.track_id == track_id:
                # Restore from recent exits
                ex.is_exited = False
                ex.last_seen = current_time_s
                self._tracks[camera_id][track_id] = ex
                self._recent_exits[camera_id].pop(i)
                return ex.visitor_id, False

        # New track — try re-entry matching first
        hist = compute_color_histogram(frame, bbox)
        reentry_match = self._match_reentry(camera_id, hist, current_time_s)

        if reentry_match is not None:
            visitor_id = reentry_match.visitor_id
            
            # If the gap is short, it's a track fragmentation, not a true re-entry
            if current_time_s - reentry_match.last_seen < 15.0:
                is_reentry = False
                logger.debug(
                    "Track continuation (fragmentation): visitor %s on %s (track %d)",
                    visitor_id, camera_id, track_id,
                )
            else:
                is_reentry = True
                logger.info(
                    "Re-entry detected: visitor %s on %s (track %d)",
                    visitor_id, camera_id, track_id,
                )
        else:
            # Try cross-camera match
            cross_match = self._match_cross_camera(camera_id, hist, current_time_s)
            if cross_match is not None:
                visitor_id = cross_match.visitor_id
                is_reentry = False
                logger.info(
                    "Cross-camera match: visitor %s on %s (track %d)",
                    visitor_id, camera_id, track_id,
                )
            else:
                visitor_id = _make_visitor_id(track_id, camera_id, current_time_s)
                is_reentry = False

        ts = TrackState(
            track_id=track_id,
            visitor_id=visitor_id,
            entry_time=current_time_s,
            last_seen=current_time_s,
            appearance_hist=hist,
        )
        self._tracks[camera_id][track_id] = ts

        # update global registry
        if visitor_id not in self._visitors:
            self._visitors[visitor_id] = VisitorRecord(
                visitor_id=visitor_id,
                first_seen=current_time_s,
                last_seen=current_time_s,
            )
        vr = self._visitors[visitor_id]
        vr.last_seen = current_time_s
        vr.camera_ids.add(camera_id)
        if hist is not None:
            vr.appearance_hist = hist

        return visitor_id, is_reentry

    def mark_exit(
        self,
        camera_id: str,
        track_id: int,
        current_time_s: float,
    ) -> Optional[str]:
        """
        Mark a track as exited.  Returns visitor_id or None.
        The TrackState is moved to the recent-exits list for potential
        re-entry matching.
        """
        cam_tracks = self._tracks.get(camera_id, {})
        ts = cam_tracks.pop(track_id, None)
        if ts is None:
            return None

        ts.is_exited = True
        ts.last_seen = current_time_s

        # keep in recent-exits
        if camera_id not in self._recent_exits:
            self._recent_exits[camera_id] = []
        self._recent_exits[camera_id].append(ts)

        # purge stale exits
        cutoff = current_time_s - self.reentry_window_s
        self._recent_exits[camera_id] = [
            e for e in self._recent_exits[camera_id] if e.last_seen >= cutoff
        ]

        return ts.visitor_id

    def get_track(self, camera_id: str, track_id: int) -> Optional[TrackState]:
        return self._tracks.get(camera_id, {}).get(track_id)

    def get_visitor(self, visitor_id: str) -> Optional[VisitorRecord]:
        return self._visitors.get(visitor_id)

    def get_session_seq(self, visitor_id: str) -> int:
        seq = self._session_seq.get(visitor_id, 0) + 1
        self._session_seq[visitor_id] = seq
        return seq

    def active_track_ids(self, camera_id: str) -> List[int]:
        """Return all active (non-exited) track IDs for a camera."""
        return list(self._tracks.get(camera_id, {}).keys())

    # ------------------------------------------------------------------
    # Re-entry matching
    # ------------------------------------------------------------------

    def _match_reentry(
        self,
        camera_id: str,
        hist: Optional[np.ndarray],
        current_time_s: float,
    ) -> Optional[TrackState]:
        """
        Search recent exits on the same camera for a matching appearance
        within the re-entry time window.
        """
        if hist is None:
            return None

        exits = self._recent_exits.get(camera_id, [])
        best_match: Optional[TrackState] = None
        best_sim: float = 0.0

        cutoff = current_time_s - self.reentry_window_s
        for ex in exits:
            if ex.last_seen < cutoff:
                continue
            if ex.appearance_hist is None:
                continue
            sim = compare_histograms(hist, ex.appearance_hist)
            if sim >= self.appearance_sim_threshold and sim > best_sim:
                best_sim = sim
                best_match = ex

        return best_match

    # ------------------------------------------------------------------
    # Cross-camera deduplication
    # ------------------------------------------------------------------

    def _match_cross_camera(
        self,
        camera_id: str,
        hist: Optional[np.ndarray],
        current_time_s: float,
    ) -> Optional[VisitorRecord]:
        """
        Search recent exits on *other* cameras for a matching appearance
        within a short time window.
        """
        if hist is None:
            return None

        best: Optional[VisitorRecord] = None
        best_sim: float = 0.0

        for other_cam, exits in self._recent_exits.items():
            if other_cam == camera_id:
                continue
            for ex in exits:
                if ex.appearance_hist is None:
                    continue
                if abs(current_time_s - ex.last_seen) > self.cross_cam_time_window_s:
                    continue
                sim = compare_histograms(hist, ex.appearance_hist)
                if sim >= self.appearance_sim_threshold and sim > best_sim:
                    best_sim = sim
                    vr = self._visitors.get(ex.visitor_id)
                    if vr is not None:
                        best = vr
                        best_sim = sim

        return best
