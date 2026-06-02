"""
pipeline/detect.py
Main detection + tracking script.

Loads YOLOv8n, processes video files frame-by-frame with ByteTrack,
classifies zones, detects staff, and emits events via EventEmitter.

Usage:
    python -m pipeline.detect \
        --input  "../dataset/CCTV Footage" \
        --output "../output/events.jsonl" \
        --store-id STORE_BLR_001
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
import torch
from ultralytics import YOLO

# Sibling modules
from pipeline.emit import EventEmitter
from pipeline.staff_classifier import StaffClassifier
from pipeline.tracker import TrackerState
from pipeline.zones import ZoneClassifier

logger = logging.getLogger("pipeline.detect")

# ---------------------------------------------------------------------------
# Device selection
# ---------------------------------------------------------------------------

DEVICE: str = "cuda" if torch.cuda.is_available() else "cpu"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_video_creation_time(video_path: str) -> Optional[datetime]:
    """Try to extract creation_time via ffprobe (if available)."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                video_path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            info = json.loads(result.stdout)
            creation = info.get("format", {}).get("tags", {}).get("creation_time")
            if creation:
                return datetime.fromisoformat(creation.replace("Z", "+00:00"))
    except Exception:
        pass
    return None


def _frame_timestamp(base: datetime, frame_idx: int, fps: float) -> str:
    """ISO-8601 timestamp for a given frame index."""
    offset = timedelta(seconds=frame_idx / max(fps, 1.0))
    return (base + offset).isoformat()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="CCTV detection pipeline — detect + track + classify + emit",
    )
    p.add_argument(
        "--input", "-i",
        required=True,
        help="Path to directory containing .mp4 video files (or a single file)",
    )
    p.add_argument(
        "--output", "-o",
        default="output/events.jsonl",
        help="Output JSONL file path (default: output/events.jsonl)",
    )
    p.add_argument(
        "--store-id",
        default="STORE_BLR_001",
        help="Store identifier (default: STORE_BLR_001)",
    )
    p.add_argument(
        "--layout",
        default=None,
        help="Path to store_layout.json (auto-detected if omitted)",
    )
    p.add_argument(
        "--api-url",
        default=None,
        help="Optional API base URL to POST events (e.g. http://localhost:8000)",
    )
    p.add_argument(
        "--skip-frames",
        type=int,
        default=3,
        help="Process every Nth frame (default: 3)",
    )
    p.add_argument(
        "--conf-threshold",
        type=float,
        default=0.35,
        help="Minimum detection confidence (default: 0.35)",
    )
    p.add_argument(
        "--base-timestamp",
        default=None,
        help="ISO-8601 timestamp used as time-zero for the first frame "
             "(overrides ffprobe metadata)",
    )
    p.add_argument(
        "--model",
        default="yolov8n.pt",
        help="YOLO model weights file (default: yolov8n.pt, auto-downloads)",
    )
    return p


# ---------------------------------------------------------------------------
# Per-video processor
# ---------------------------------------------------------------------------

def process_video(
    video_path: Path,
    *,
    model: YOLO,
    emitter: EventEmitter,
    zone_cls: ZoneClassifier,
    staff_cls: StaffClassifier,
    tracker: TrackerState,
    store_id: str,
    skip_frames: int,
    conf_threshold: float,
    base_timestamp: Optional[datetime],
) -> int:
    """
    Process a single video file.  Returns the number of events emitted.
    """
    filename = video_path.name
    camera_id = zone_cls.camera_id_for_file(filename)
    if camera_id is None:
        logger.warning("No camera mapping for '%s' — skipping", filename)
        return 0

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.error("Cannot open video: %s", video_path)
        return 0

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # Determine base timestamp
    if base_timestamp:
        t0 = base_timestamp
    else:
        t0 = _get_video_creation_time(str(video_path))
        if t0 is None:
            t0 = datetime.now(timezone.utc)
            logger.info("Using current UTC time as base timestamp for %s", filename)

    logger.info(
        "Processing %s — camera=%s, %dx%d, %.1f fps, %d frames",
        filename, camera_id, frame_w, frame_h, fps, total_frames,
    )

    events_before = emitter.total_emitted
    frame_idx = 0
    prev_track_ids: set[int] = set()

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        current_time_s = frame_idx / max(fps, 1.0)
        timestamp_iso = _frame_timestamp(t0, frame_idx, fps)

        # Skip frames for speed
        if frame_idx % skip_frames != 0:
            frame_idx += 1
            continue

        # --- Run YOLO tracking ---
        results = model.track(
            frame,
            persist=True,
            tracker="bytetrack.yaml",
            conf=conf_threshold,
            classes=[0],  # person class only
            verbose=False,
            device=DEVICE,
        )

        current_track_ids: set[int] = set()

        if results and results[0].boxes is not None and len(results[0].boxes) > 0:
            boxes = results[0].boxes

            for i in range(len(boxes)):
                # Extract detection
                xyxy = boxes.xyxy[i].cpu().numpy()
                x1, y1, x2, y2 = float(xyxy[0]), float(xyxy[1]), float(xyxy[2]), float(xyxy[3])
                conf = float(boxes.conf[i].cpu().numpy())

                # Track ID
                if boxes.id is not None:
                    track_id = int(boxes.id[i].cpu().numpy())
                else:
                    continue  # skip detections without track id

                current_track_ids.add(track_id)
                bbox = (x1, y1, x2, y2)
                bbox_int = (int(x1), int(y1), int(x2), int(y2))

                # --- Tracker update (visitor ID + re-entry) ---
                visitor_id, is_reentry = tracker.update(
                    camera_id=camera_id,
                    track_id=track_id,
                    frame=frame,
                    bbox=bbox_int,
                    current_time_s=current_time_s,
                )

                # --- Staff classification ---
                is_staff, staff_conf = staff_cls.classify(
                    frame=frame,
                    bbox=bbox_int,
                    track_id=track_id,
                    current_time_s=current_time_s,
                )

                # update track state
                ts = tracker.get_track(camera_id, track_id)
                if ts is not None:
                    ts.is_staff = is_staff

                # --- Re-entry event ---
                if is_reentry:
                    emitter.emit_reentry(
                        store_id=store_id,
                        camera_id=camera_id,
                        visitor_id=visitor_id,
                        timestamp=timestamp_iso,
                        confidence=conf,
                        is_staff=is_staff,
                    )

                # --- Zone classification ---
                zone_events = zone_cls.classify(
                    camera_id=camera_id,
                    track_id=track_id,
                    bbox=bbox,
                    frame_w=frame_w,
                    frame_h=frame_h,
                    current_time_s=current_time_s,
                )

                for ze in zone_events:
                    et = ze["event_type"]
                    zone_id = ze.get("zone_id")
                    dwell_ms = ze.get("dwell_ms", 0)
                    queue_depth = ze.get("queue_depth")

                    if et == "ENTRY":
                        emitter.emit_entry(
                            store_id=store_id,
                            camera_id=camera_id,
                            visitor_id=visitor_id,
                            timestamp=timestamp_iso,
                            confidence=conf,
                            is_staff=is_staff,
                        )
                    elif et == "EXIT":
                        emitter.emit_exit(
                            store_id=store_id,
                            camera_id=camera_id,
                            visitor_id=visitor_id,
                            timestamp=timestamp_iso,
                            confidence=conf,
                            dwell_ms=dwell_ms,
                            is_staff=is_staff,
                        )
                    elif et == "ZONE_ENTER":
                        emitter.emit_zone_enter(
                            store_id=store_id,
                            camera_id=camera_id,
                            visitor_id=visitor_id,
                            timestamp=timestamp_iso,
                            zone_id=zone_id or "",
                            confidence=conf,
                            is_staff=is_staff,
                            sku_zone=zone_id,
                        )
                    elif et == "ZONE_EXIT":
                        emitter.emit_zone_exit(
                            store_id=store_id,
                            camera_id=camera_id,
                            visitor_id=visitor_id,
                            timestamp=timestamp_iso,
                            zone_id=zone_id or "",
                            confidence=conf,
                            dwell_ms=dwell_ms,
                            is_staff=is_staff,
                        )
                    elif et == "ZONE_DWELL":
                        emitter.emit_zone_dwell(
                            store_id=store_id,
                            camera_id=camera_id,
                            visitor_id=visitor_id,
                            timestamp=timestamp_iso,
                            zone_id=zone_id or "",
                            dwell_ms=dwell_ms,
                            confidence=conf,
                            is_staff=is_staff,
                        )
                    elif et == "BILLING_QUEUE_JOIN":
                        emitter.emit_billing_queue_join(
                            store_id=store_id,
                            camera_id=camera_id,
                            visitor_id=visitor_id,
                            timestamp=timestamp_iso,
                            confidence=conf,
                            queue_depth=queue_depth or 0,
                            is_staff=is_staff,
                        )
                    elif et == "BILLING_QUEUE_ABANDON":
                        emitter.emit_billing_queue_abandon(
                            store_id=store_id,
                            camera_id=camera_id,
                            visitor_id=visitor_id,
                            timestamp=timestamp_iso,
                            confidence=conf,
                            dwell_ms=dwell_ms,
                            queue_depth=queue_depth or 0,
                            is_staff=is_staff,
                        )

        # --- Handle lost tracks (people who disappeared) ---
        lost_ids = prev_track_ids - current_track_ids
        for lost_id in lost_ids:
            # Let the zone classifier clean up
            zone_cleanup = zone_cls.track_lost(camera_id, lost_id, current_time_s)
            visitor_id_lost = tracker.mark_exit(camera_id, lost_id, current_time_s)

            if visitor_id_lost:
                ts_lost = None
                # try to get staff flag from tracker's recent exits
                is_staff_lost = False

                for ze in zone_cleanup:
                    et = ze["event_type"]
                    zone_id = ze.get("zone_id")
                    dwell_ms = ze.get("dwell_ms", 0)
                    queue_depth = ze.get("queue_depth")

                    if et == "ZONE_EXIT":
                        emitter.emit_zone_exit(
                            store_id=store_id,
                            camera_id=camera_id,
                            visitor_id=visitor_id_lost,
                            timestamp=timestamp_iso,
                            zone_id=zone_id or "",
                            confidence=0.5,
                            dwell_ms=dwell_ms,
                            is_staff=is_staff_lost,
                        )
                    elif et == "BILLING_QUEUE_ABANDON":
                        emitter.emit_billing_queue_abandon(
                            store_id=store_id,
                            camera_id=camera_id,
                            visitor_id=visitor_id_lost,
                            timestamp=timestamp_iso,
                            confidence=0.5,
                            dwell_ms=dwell_ms,
                            queue_depth=queue_depth or 0,
                            is_staff=is_staff_lost,
                        )

        prev_track_ids = current_track_ids
        frame_idx += 1

        # Progress to stderr
        if frame_idx % (skip_frames * 100) == 0:
            pct = (frame_idx / max(total_frames, 1)) * 100
            print(
                f"\r  [{camera_id}] {frame_idx}/{total_frames} "
                f"({pct:.1f}%) — events: {emitter.total_emitted - events_before}",
                end="",
                file=sys.stderr,
            )

    cap.release()
    events_emitted = emitter.total_emitted - events_before
    print(
        f"\n  [{camera_id}] Done — {events_emitted} events from {frame_idx} frames",
        file=sys.stderr,
    )
    return events_emitted


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s  %(message)s",
        stream=sys.stderr,
    )

    logger.info("Device: %s", DEVICE)

    # Resolve paths
    project_root = Path(__file__).resolve().parent.parent
    input_path = Path(args.input)
    output_path = Path(args.output)
    layout_path = Path(args.layout) if args.layout else project_root / "data" / "store_layout.json"

    if not layout_path.exists():
        logger.error("store_layout.json not found at %s", layout_path)
        sys.exit(1)

    # Collect video files
    if input_path.is_file():
        video_files = [input_path]
    elif input_path.is_dir():
        video_files = sorted(input_path.glob("*.mp4"))
    else:
        logger.error("Input path does not exist: %s", input_path)
        sys.exit(1)

    if not video_files:
        logger.error("No .mp4 files found in %s", input_path)
        sys.exit(1)

    logger.info("Found %d video file(s) in %s", len(video_files), input_path)

    # Load model (auto-downloads yolov8n.pt if missing)
    logger.info("Loading YOLOv8n model (%s)…", args.model)
    model = YOLO(args.model)

    # Parse base timestamp
    base_ts: Optional[datetime] = None
    if args.base_timestamp:
        try:
            base_ts = datetime.fromisoformat(args.base_timestamp.replace("Z", "+00:00"))
        except ValueError:
            logger.error("Invalid --base-timestamp format: %s", args.base_timestamp)
            sys.exit(1)

    # Initialise components
    zone_cls = ZoneClassifier(layout_path)
    staff_cls = StaffClassifier()
    tracker = TrackerState()
    emitter = EventEmitter(output_path=output_path, api_url=args.api_url)

    total_events = 0
    t_start = time.monotonic()

    for vf in video_files:
        logger.info("=== Processing: %s ===", vf.name)
        n = process_video(
            vf,
            model=model,
            emitter=emitter,
            zone_cls=zone_cls,
            staff_cls=staff_cls,
            tracker=tracker,
            store_id=args.store_id,
            skip_frames=args.skip_frames,
            conf_threshold=args.conf_threshold,
            base_timestamp=base_ts,
        )
        total_events += n

    emitter.close()
    elapsed = time.monotonic() - t_start
    logger.info(
        "Pipeline complete: %d events in %.1f s → %s",
        total_events, elapsed, output_path,
    )


if __name__ == "__main__":
    main()
