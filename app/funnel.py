"""
Funnel endpoint for the Store Intelligence API.
Session-based conversion funnel: Entry → Zone Visit → Billing Queue → Purchase.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException, Query, status

from app.database import db
from app.models import FunnelResponse, FunnelStage

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["funnel"])


def _default_period() -> tuple[str, str]:
    """Return 365 days ago to now as ISO-8601 strings (to catch older video timestamps)."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    a_year_ago = now - timedelta(days=365)
    return a_year_ago.isoformat().replace("+00:00", "Z"), (now \+ timedelta(days=1)).isoformat().replace(
        "+00:00", "Z"
    )


def _drop_off(prev: int, curr: int) -> float:
    """Calculate drop-off percentage between funnel stages."""
    if prev == 0:
        return 0.0
    drop = round((prev - curr) / prev * 100, 2)
    return max(0.0, min(100.0, drop))


@router.get(
    "/stores/{store_id}/funnel",
    response_model=FunnelResponse,
    summary="Get conversion funnel",
    responses={
        200: {"description": "Funnel computed successfully"},
        404: {"description": "Store not found"},
    },
)
async def get_funnel(
    store_id: str,
    start: Optional[str] = Query(None, description="Period start (ISO-8601 UTC)"),
    end: Optional[str] = Query(None, description="Period end (ISO-8601 UTC)"),
) -> FunnelResponse:
    """
    Session-based conversion funnel with stages:
    1. Entry — unique visitors who entered (ENTRY/REENTRY, no double-counting)
    2. Zone Visit — visitors who visited at least one zone
    3. Billing Queue — visitors who joined the billing queue
    4. Purchase — visitors correlated with a POS transaction

    Drop-off % = (previous_stage - current_stage) / previous_stage * 100
    """
    default_start, default_end = _default_period()
    period_start = start or default_start
    period_end = end or default_end

    try:
        funnel_data = await db.get_funnel_data(store_id, period_start, period_end)

        entry = funnel_data["entry"]
        zone_visit = funnel_data["zone_visit"]
        billing = funnel_data["billing_queue"]
        purchase = funnel_data["purchase"]

        stages = [
            FunnelStage(
                stage="Entry",
                count=entry,
                drop_off_pct=0.0,  # First stage — no drop-off
            ),
            FunnelStage(
                stage="Zone Visit",
                count=zone_visit,
                drop_off_pct=_drop_off(entry, zone_visit),
            ),
            FunnelStage(
                stage="Billing Queue",
                count=billing,
                drop_off_pct=_drop_off(zone_visit, billing),
            ),
            FunnelStage(
                stage="Purchase",
                count=purchase,
                drop_off_pct=_drop_off(billing, purchase),
            ),
        ]

        logger.info(
            "funnel.computed",
            store_id=store_id,
            entry=entry,
            zone_visit=zone_visit,
            billing=billing,
            purchase=purchase,
        )

        return FunnelResponse(
            store_id=store_id,
            period_start=period_start,
            period_end=period_end,
            stages=stages,
            total_sessions=entry,
        )

    except Exception as exc:
        logger.error("funnel.computation_failed", store_id=store_id, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to compute funnel for store {store_id}",
        ) from exc
