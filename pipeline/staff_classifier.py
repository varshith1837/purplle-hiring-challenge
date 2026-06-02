"""
pipeline/staff_classifier.py
Staff vs. customer classification using upper-body colour histogram
analysis and a long-presence heuristic fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import cv2
import numpy as np

logger = logging.getLogger("pipeline.staff_classifier")


# ---------------------------------------------------------------------------
# Configurable staff uniform colour ranges (HSV)
# ---------------------------------------------------------------------------
# Default: dark-coloured polo/shirt typical of retail uniforms.
# Format: list of (h_low, s_low, v_low, h_high, s_high, v_high).
DEFAULT_UNIFORM_RANGES_HSV: list[Tuple[int, int, int, int, int, int]] = [
    # Dark clothing (black / dark grey)
    (0, 0, 0, 180, 255, 80),
    # Dark navy / blue
    (100, 50, 20, 130, 255, 100),
    # Dark maroon / wine
    (0, 50, 20, 10, 255, 100),
    (170, 50, 20, 180, 255, 100),
]

# Fraction of the upper body that must match uniform colours to classify as
# staff (tunable).
UNIFORM_PIXEL_RATIO_THRESHOLD: float = 0.40

# If a track has been visible for more than this fraction of total video
# duration so far, it is considered staff (fallback heuristic).
LONG_PRESENCE_FRACTION: float = 0.70


# ---------------------------------------------------------------------------
# StaffClassifier
# ---------------------------------------------------------------------------

@dataclass
class _TrackPresence:
    first_seen_s: float = 0.0
    frames_seen: int = 0


class StaffClassifier:
    """
    Determines whether a detected person is *staff* or *customer*.

    Primary signal: upper-body HSV colour histogram matched against a
    configurable staff-uniform profile.

    Fallback: if a ``track_id`` has been present for > 70 % of the elapsed
    video duration it is classified as staff regardless of colour.
    """

    def __init__(
        self,
        uniform_ranges: Optional[list[Tuple[int, int, int, int, int, int]]] = None,
        pixel_ratio_threshold: float = UNIFORM_PIXEL_RATIO_THRESHOLD,
        long_presence_fraction: float = LONG_PRESENCE_FRACTION,
    ) -> None:
        self.uniform_ranges = uniform_ranges or DEFAULT_UNIFORM_RANGES_HSV
        self.pixel_ratio_threshold = pixel_ratio_threshold
        self.long_presence_fraction = long_presence_fraction

        # per-track presence accounting
        self._presence: Dict[int, _TrackPresence] = {}
        self._total_frames: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def classify(
        self,
        frame: np.ndarray,
        bbox: Tuple[int, int, int, int],
        track_id: int = -1,
        current_time_s: float = 0.0,
    ) -> Tuple[bool, float]:
        """
        Classify a detected person.

        Parameters
        ----------
        frame : np.ndarray  (BGR, HxWx3)
            The full video frame.
        bbox : tuple[int, int, int, int]
            ``(x1, y1, x2, y2)`` in pixel coords.
        track_id : int
            The track identifier (for presence heuristic).
        current_time_s : float
            Elapsed seconds since the start of the video.

        Returns
        -------
        (is_staff, confidence) : (bool, float)
        """
        self._total_frames += 1

        # Update presence
        if track_id >= 0:
            if track_id not in self._presence:
                self._presence[track_id] = _TrackPresence(
                    first_seen_s=current_time_s,
                    frames_seen=0,
                )
            self._presence[track_id].frames_seen += 1

        # --- Colour-based classification ---
        colour_is_staff, colour_conf = self._classify_by_colour(frame, bbox)

        # --- Long-presence fallback ---
        presence_is_staff = False
        presence_conf = 0.0
        if track_id >= 0 and self._total_frames > 30:
            tp = self._presence.get(track_id)
            if tp is not None:
                ratio = tp.frames_seen / max(self._total_frames, 1)
                if ratio >= self.long_presence_fraction:
                    presence_is_staff = True
                    presence_conf = min(ratio, 1.0)

        # Combine: colour signal takes priority when high-confidence
        if colour_conf >= 0.50:
            return colour_is_staff, colour_conf
        elif presence_is_staff:
            return True, presence_conf
        else:
            return colour_is_staff, colour_conf

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _classify_by_colour(
        self,
        frame: np.ndarray,
        bbox: Tuple[int, int, int, int],
    ) -> Tuple[bool, float]:
        """
        Extract the upper 40 % of the bounding box (torso region), convert to
        HSV, and check how many pixels fall within the uniform colour ranges.
        """
        x1, y1, x2, y2 = bbox
        h_frame, w_frame = frame.shape[:2]

        # clamp
        x1 = max(0, int(x1))
        y1 = max(0, int(y1))
        x2 = min(w_frame, int(x2))
        y2 = min(h_frame, int(y2))

        if x2 <= x1 or y2 <= y1:
            return False, 0.0

        # upper 40 % of bbox height = torso
        torso_h = int((y2 - y1) * 0.40)
        torso_y2 = y1 + max(torso_h, 1)
        torso = frame[y1:torso_y2, x1:x2]

        if torso.size == 0:
            return False, 0.0

        hsv = cv2.cvtColor(torso, cv2.COLOR_BGR2HSV)
        total_pixels = hsv.shape[0] * hsv.shape[1]
        if total_pixels == 0:
            return False, 0.0

        # Build combined mask for all uniform colour ranges
        combined_mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
        for h_lo, s_lo, v_lo, h_hi, s_hi, v_hi in self.uniform_ranges:
            lower = np.array([h_lo, s_lo, v_lo], dtype=np.uint8)
            upper = np.array([h_hi, s_hi, v_hi], dtype=np.uint8)
            mask = cv2.inRange(hsv, lower, upper)
            combined_mask = cv2.bitwise_or(combined_mask, mask)

        match_ratio = float(np.count_nonzero(combined_mask)) / total_pixels

        is_staff = match_ratio >= self.pixel_ratio_threshold
        confidence = min(match_ratio / self.pixel_ratio_threshold, 1.0) if is_staff else match_ratio

        return is_staff, round(confidence, 4)
