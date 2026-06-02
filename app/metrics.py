"""
Metrics endpoint for the Store Intelligence API.
Real-time computation of store performance metrics.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException, Query, status

from app.database import db
from app.models import MetricsResponse, ZoneDwell

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["metrics"])


def _default_period() -> tuple[str, str]:
    """Return 30 days ago to now as ISO-8601 strings (to catch older video timestamps)."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=365)
    return thirty_days_ago.isoformat().replace("+00:00", "Z"), (now + timedelta(days=1)).isoformat().replace(
        "+00:00", "Z"
    )


@router.get(
    "/stores/{store_id}/metrics",
    response_model=MetricsResponse,
    summary="Get real-time store metrics",
    responses={
        200: {"description": "Metrics computed successfully"},
        404: {"description": "Store not found"},
    },
)
async def get_metrics(
    store_id: str,
    start: Optional[str] = Query(None, description="Period start (ISO-8601 UTC)"),
    end: Optional[str] = Query(None, description="Period end (ISO-8601 UTC)"),
) -> MetricsResponse:
    """
    Real-time computation of store metrics including:
    - Unique visitors (excluding staff)
    - Conversion rate (visitors who purchased / total unique visitors)
    - Average dwell time per zone
    - Current billing queue depth
    - Billing queue abandonment rate
    - Total POS transactions
    """
    default_start, default_end = _default_period()
    period_start = start or default_start
    period_end = end or default_end

    try:
        # --- Unique visitors ---
        unique_visitors = await db.get_unique_visitors(
            store_id, period_start, period_end
        )

        # --- Avg dwell by zone ---
        dwell_rows = await db.get_avg_dwell_by_zone(
            store_id, period_start, period_end
        )
        avg_dwell_by_zone = [
            ZoneDwell(
                zone_id=row["zone_id"],
                avg_dwell_ms=round(row["avg_dwell_ms"], 2),
                visit_count=row["visit_count"],
            )
            for row in dwell_rows
        ]

        # --- Current queue depth ---
        current_queue_depth = await db.get_current_queue_depth(store_id)

        # --- Abandonment rate ---
        abandonment_rate = await db.get_abandonment_rate(
            store_id, period_start, period_end
        )

        # --- Total transactions ---
        total_transactions = await db.get_total_transactions(
            store_id, period_start, period_end
        )

        # --- Conversion rate ---
        # Conversion rate = visitors who purchased / total unique visitors
        if unique_visitors == 0:
            conversion_rate = 0.0
        else:
            # Get purchase count via funnel data helper
            purchase_count = await db._get_purchase_count(
                store_id, period_start, period_end
            )
            conversion_rate = round(
                min(purchase_count / unique_visitors, 1.0), 4
            )

        logger.info(
            "metrics.computed",
            store_id=store_id,
            unique_visitors=unique_visitors,
            conversion_rate=conversion_rate,
            total_transactions=total_transactions,
        )

        return MetricsResponse(
            store_id=store_id,
            period_start=period_start,
            period_end=period_end,
            unique_visitors=unique_visitors,
            conversion_rate=conversion_rate,
            avg_dwell_by_zone=avg_dwell_by_zone,
            current_queue_depth=current_queue_depth,
            abandonment_rate=abandonment_rate,
            total_transactions=total_transactions,
        )

    except Exception as exc:
        logger.error("metrics.computation_failed", store_id=store_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to compute metrics for store {store_id}",
        ) from exc

@router.get("/stores/{store_id}/events")
async def get_recent_events(store_id: str, limit: int = 20):
    query = "SELECT event_id, store_id, camera_id, visitor_id, event_type, timestamp, zone_id, dwell_ms, is_staff, confidence, queue_depth, sku_zone, session_seq FROM events WHERE store_id = ? ORDER BY timestamp DESC LIMIT ?"
    
    cursor = await db.db.execute(query, (store_id, limit))
    rows = await cursor.fetchall()
            
    events = []
    for r in rows:
        events.append({
            "event_id": r["event_id"],
            "store_id": r["store_id"],
            "camera_id": r["camera_id"],
            "visitor_id": r["visitor_id"],
            "event_type": r["event_type"],
            "timestamp": r["timestamp"],
            "zone_id": r["zone_id"],
            "dwell_ms": r["dwell_ms"],
            "is_staff": bool(r["is_staff"]),
            "confidence": r["confidence"],
            "metadata": {
                "queue_depth": r["queue_depth"],
                "sku_zone": r["sku_zone"],
                "session_seq": r["session_seq"],
            }
        })
    return events
