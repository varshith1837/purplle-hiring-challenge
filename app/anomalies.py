"""
Anomaly detection endpoint for the Store Intelligence API.
Detects queue spikes, conversion drops, and dead zones.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException, status

from app.database import db
from app.models import AnomaliesResponse, Anomaly, AnomalyType, Severity

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["anomalies"])

# Cache the store layout for zone lookups
_store_layout: Optional[dict] = None


def _load_store_layout() -> dict:
    """Load store layout from JSON file (cached)."""
    global _store_layout
    if _store_layout is None:
        layout_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)), "data", "store_layout.json"
        )
        try:
            with open(layout_path, "r", encoding="utf-8") as f:
                _store_layout = json.load(f)
        except Exception:
            _store_layout = {"stores": []}
    return _store_layout


def _get_store_zones(store_id: str) -> list[str]:
    """Get zone IDs for a store from the layout."""
    layout = _load_store_layout()
    for store in layout.get("stores", []):
        if store["store_id"] == store_id:
            return [z["zone_id"] for z in store.get("zones", [])]
    return []


@router.get(
    "/stores/{store_id}/anomalies",
    response_model=AnomaliesResponse,
    summary="Detect active anomalies",
    responses={
        200: {"description": "Anomaly detection completed"},
    },
)
async def get_anomalies(store_id: str) -> AnomaliesResponse:
    """
    Detect active anomalies for a store:

    1. **BILLING_QUEUE_SPIKE**: Current queue > 2× average queue depth
       - Severity: CRITICAL if queue > 5, WARN if queue > 3
    2. **CONVERSION_DROP**: Today's conversion < 70% of 7-day average
       - Severity: CRITICAL if drop > 50%, WARN if drop > 30%
    3. **DEAD_ZONE**: No visits to a zone in the last 30 minutes
       - Severity: WARN
    """
    now = datetime.now(timezone.utc)
    anomalies: list[Anomaly] = []

    try:
        # -----------------------------------------------------------------
        # 1. BILLING_QUEUE_SPIKE
        # -----------------------------------------------------------------
        await _detect_queue_spike(store_id, anomalies, now)

        # -----------------------------------------------------------------
        # 2. CONVERSION_DROP
        # -----------------------------------------------------------------
        await _detect_conversion_drop(store_id, anomalies, now)

        # -----------------------------------------------------------------
        # 3. DEAD_ZONE
        # -----------------------------------------------------------------
        await _detect_dead_zones(store_id, anomalies, now)

        logger.info(
            "anomalies.detected",
            store_id=store_id,
            count=len(anomalies),
        )

        return AnomaliesResponse(
            store_id=store_id,
            active_anomalies=anomalies,
        )

    except Exception as exc:
        logger.error(
            "anomalies.detection_failed", store_id=store_id, error=str(exc)
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to detect anomalies for store {store_id}",
        ) from exc


async def _detect_queue_spike(
    store_id: str, anomalies: list[Anomaly], now: datetime
) -> None:
    """Detect billing queue spikes."""
    try:
        queue_depths = await db.get_recent_queue_depths(store_id, minutes=30)
        if not queue_depths:
            return

        current_depth = queue_depths[0]  # Most recent
        avg_depth = sum(queue_depths) / len(queue_depths) if queue_depths else 0

        # Spike if current > 2× average
        if avg_depth > 0 and current_depth > 2 * avg_depth:
            if current_depth > 5:
                severity = Severity.CRITICAL
            elif current_depth > 3:
                severity = Severity.WARN
            else:
                severity = Severity.INFO

            anomalies.append(
                Anomaly(
                    anomaly_type=AnomalyType.BILLING_QUEUE_SPIKE,
                    severity=severity,
                    zone_id="BILLING",
                    detail=(
                        f"Current queue depth ({current_depth}) is "
                        f"{current_depth / avg_depth:.1f}× the average ({avg_depth:.1f})"
                    ),
                    suggested_action="Open additional billing counter or deploy staff to assist queue",
                    detected_at=(now \+ timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                )
            )
    except Exception as exc:
        logger.warning("anomalies.queue_spike_check_failed", error=str(exc))


async def _detect_conversion_drop(
    store_id: str, anomalies: list[Anomaly], now: datetime
) -> None:
    """Detect conversion rate drops compared to 7-day average."""
    try:
        # Today's window
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_start_str = today_start.isoformat().replace("+00:00", "Z")
        now_str = (now \+ timedelta(days=1)).isoformat().replace("+00:00", "Z")

        # Today's conversion
        today_visitors = await db.get_unique_visitors(
            store_id, today_start_str, now_str
        )
        if today_visitors == 0:
            return  # No data today, nothing to compare

        today_purchases = await db._get_purchase_count(
            store_id, today_start_str, now_str
        )
        today_conversion = today_purchases / today_visitors

        # 7-day average conversion (same time-of-day window for each day)
        total_conversion = 0.0
        valid_days = 0
        for day_offset in range(1, 8):
            day_start = (today_start - timedelta(days=day_offset))
            day_end = day_start + (now - today_start)
            day_start_str = day_start.isoformat().replace("+00:00", "Z")
            day_end_str = day_end.isoformat().replace("+00:00", "Z")

            day_visitors = await db.get_unique_visitors(
                store_id, day_start_str, day_end_str
            )
            if day_visitors == 0:
                continue
            day_purchases = await db._get_purchase_count(
                store_id, day_start_str, day_end_str
            )
            total_conversion += day_purchases / day_visitors
            valid_days += 1

        if valid_days == 0:
            return  # No historical data

        avg_7day_conversion = total_conversion / valid_days

        if avg_7day_conversion == 0:
            return

        drop_pct = (1 - today_conversion / avg_7day_conversion) * 100

        if today_conversion < 0.7 * avg_7day_conversion:
            if drop_pct > 50:
                severity = Severity.CRITICAL
            elif drop_pct > 30:
                severity = Severity.WARN
            else:
                severity = Severity.INFO

            anomalies.append(
                Anomaly(
                    anomaly_type=AnomalyType.CONVERSION_DROP,
                    severity=severity,
                    detail=(
                        f"Today's conversion rate ({today_conversion:.2%}) is "
                        f"{drop_pct:.1f}% below the 7-day average ({avg_7day_conversion:.2%})"
                    ),
                    suggested_action="Review store layout, check for stock issues, or increase staff assistance on the floor",
                    detected_at=(now \+ timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                )
            )
    except Exception as exc:
        logger.warning("anomalies.conversion_drop_check_failed", error=str(exc))


async def _detect_dead_zones(
    store_id: str, anomalies: list[Anomaly], now: datetime
) -> None:
    """Detect zones with no visits in the last 30 minutes."""
    try:
        # Get all configured zones for this store (exclude ENTRY and BILLING)
        configured_zones = _get_store_zones(store_id)
        monitoring_zones = [
            z for z in configured_zones if z not in ("ENTRY", "BILLING")
        ]

        if not monitoring_zones:
            return

        last_visits = await db.get_zone_last_visit(store_id)
        threshold = now - timedelta(minutes=30)

        for zone_id in monitoring_zones:
            last_visit_str = last_visits.get(zone_id)

            is_dead = False
            if last_visit_str is None:
                is_dead = True
                detail = f"Zone '{zone_id}' has received no visits"
            else:
                try:
                    last_visit_dt = datetime.fromisoformat(
                        last_visit_str.replace("Z", "+00:00")
                    )
                    if last_visit_dt.tzinfo is None:
                        last_visit_dt = last_visit_dt.replace(tzinfo=timezone.utc)
                    if last_visit_dt < threshold:
                        is_dead = True
                        elapsed_min = int((now - last_visit_dt).total_seconds() / 60)
                        detail = (
                            f"Zone '{zone_id}' has had no visits for "
                            f"{elapsed_min} minutes (last: {last_visit_str})"
                        )
                except (ValueError, TypeError):
                    is_dead = True
                    detail = f"Zone '{zone_id}' — unable to determine last visit time"

            if is_dead:
                anomalies.append(
                    Anomaly(
                        anomaly_type=AnomalyType.DEAD_ZONE,
                        severity=Severity.WARN,
                        zone_id=zone_id,
                        detail=detail,
                        suggested_action=f"Investigate zone '{zone_id}' — consider repositioning products or adding signage",
                        detected_at=(now \+ timedelta(days=1)).isoformat().replace("+00:00", "Z"),
                    )
                )
    except Exception as exc:
        logger.warning("anomalies.dead_zone_check_failed", error=str(exc))
