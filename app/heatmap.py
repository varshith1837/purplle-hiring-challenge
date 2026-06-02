"""
Heatmap endpoint for the Store Intelligence API.
Returns zone visit frequency, average dwell, and normalised intensity.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException, Query, status

from app.database import db
from app.models import HeatmapResponse, ZoneHeat

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["heatmap"])

# Cache zone name lookup
_zone_name_map: Optional[dict[str, dict[str, str]]] = None


def _load_zone_names() -> dict[str, dict[str, str]]:
    """
    Load zone names from store_layout.json.
    Returns {store_id: {zone_id: zone_name}}.
    """
    global _zone_name_map
    if _zone_name_map is not None:
        return _zone_name_map

    layout_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "data", "store_layout.json"
    )
    _zone_name_map = {}
    try:
        with open(layout_path, "r", encoding="utf-8") as f:
            layout = json.load(f)
        for store in layout.get("stores", []):
            sid = store["store_id"]
            _zone_name_map[sid] = {
                z["zone_id"]: z["zone_name"] for z in store.get("zones", [])
            }
    except Exception as exc:
        logger.warning("heatmap.layout_load_failed", error=str(exc))

    return _zone_name_map


def _default_period() -> tuple[str, str]:
    """Return 365 days ago to now as ISO-8601 strings (to catch older video timestamps)."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    a_year_ago = now - timedelta(days=365)
    return a_year_ago.isoformat().replace("+00:00", "Z"), (now + timedelta(days=1)).isoformat().replace(
        "+00:00", "Z"
    )


@router.get(
    "/stores/{store_id}/heatmap",
    response_model=HeatmapResponse,
    summary="Get zone heatmap",
    responses={
        200: {"description": "Heatmap computed successfully"},
    },
)
async def get_heatmap(
    store_id: str,
    start: Optional[str] = Query(None, description="Period start (ISO-8601 UTC)"),
    end: Optional[str] = Query(None, description="Period end (ISO-8601 UTC)"),
) -> HeatmapResponse:
    """
    Returns zone visit frequency + average dwell with intensity normalised 0–100.
    Includes data_confidence='LOW' if fewer than 20 sessions in the window.
    """
    default_start, default_end = _default_period()
    period_start = start or default_start
    period_end = end or default_end

    try:
        zone_rows = await db.get_zone_heatmap(store_id, period_start, period_end)

        # Load zone names
        zone_names = _load_zone_names().get(store_id, {})

        # Total sessions for data confidence
        total_sessions = sum(row["visit_count"] for row in zone_rows)

        # Normalise intensity to 0–100
        max_visits = max((row["visit_count"] for row in zone_rows), default=0)

        zones: list[ZoneHeat] = []
        for row in zone_rows:
            visit_count = row["visit_count"]
            avg_dwell = row["avg_dwell_ms"] if row["avg_dwell_ms"] is not None else 0.0

            # Normalise
            if max_visits > 0:
                intensity = round(visit_count / max_visits * 100, 2)
            else:
                intensity = 0.0

            # Data confidence
            confidence = "HIGH" if total_sessions >= 20 else "LOW"

            zone_id = row["zone_id"]
            zone_name = zone_names.get(zone_id, zone_id)

            zones.append(
                ZoneHeat(
                    zone_id=zone_id,
                    zone_name=zone_name,
                    visit_count=visit_count,
                    avg_dwell_ms=round(avg_dwell, 2),
                    intensity=intensity,
                    data_confidence=confidence,
                )
            )

        logger.info(
            "heatmap.computed",
            store_id=store_id,
            zone_count=len(zones),
            total_sessions=total_sessions,
        )

        return HeatmapResponse(
            store_id=store_id,
            period_start=period_start,
            period_end=period_end,
            zones=zones,
        )

    except Exception as exc:
        logger.error("heatmap.computation_failed", store_id=store_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to compute heatmap for store {store_id}",
        ) from exc
