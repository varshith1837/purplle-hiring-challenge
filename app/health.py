"""
Health check endpoint for the Store Intelligence API.
Reports uptime, database connectivity, and per-store health status.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Response, status

from app.database import db
from app.models import HealthResponse, StoreHealth

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])

# Track application startup time
_startup_time: float = time.time()


def set_startup_time() -> None:
    """Called during app startup to record the start time."""
    global _startup_time
    _startup_time = time.time()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Health check",
    responses={
        200: {"description": "Service is healthy"},
        503: {"description": "Service is degraded or database unavailable"},
    },
)
async def health_check(response: Response) -> HealthResponse:
    """
    Returns the health status of the API including:
    - Overall status (UP / DEGRADED)
    - Uptime in seconds
    - Database connectivity
    - Per-store health with STALE_FEED warnings
    """
    uptime = round(time.time() - _startup_time, 2)
    overall_status = "UP"
    db_status = "OK"

    # --- Database connectivity check ---
    try:
        db_ok = await db.check_connection()
        if not db_ok:
            raise RuntimeError("Database check returned False")
    except Exception as exc:
        logger.error("health.db_check_failed", error=str(exc))
        db_status = "UNAVAILABLE"
        overall_status = "DEGRADED"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(
            status=overall_status,
            uptime_seconds=uptime,
            database=db_status,
            stores=[],
        )

    # --- Per-store health ---
    stores: list[StoreHealth] = []
    try:
        store_rows = await db.get_store_health()
        now = datetime.now(timezone.utc)

        for row in store_rows:
            warnings: list[str] = []
            store_status = "OK"
            last_event_at = row.get("last_event_at")

            if last_event_at:
                try:
                    last_dt = datetime.fromisoformat(
                        last_event_at.replace("Z", "+00:00")
                    )
                    # Make tz-aware if naive
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    elapsed = (now - last_dt).total_seconds()
                    if elapsed > 600:  # 10 minutes
                        warnings.append(
                            f"STALE_FEED: No events for {int(elapsed)}s "
                            f"(last: {last_event_at})"
                        )
                        store_status = "WARN"
                except (ValueError, TypeError):
                    warnings.append("STALE_FEED: Unable to parse last_event_at")
                    store_status = "WARN"
            else:
                warnings.append("STALE_FEED: No events received yet")
                store_status = "WARN"

            stores.append(
                StoreHealth(
                    store_id=row["store_id"],
                    last_event_at=last_event_at,
                    event_count=row.get("event_count", 0),
                    status=store_status,
                    warnings=warnings,
                )
            )
    except Exception as exc:
        logger.error("health.store_health_failed", error=str(exc))
        overall_status = "DEGRADED"

    return HealthResponse(
        status=overall_status,
        uptime_seconds=uptime,
        database=db_status,
        stores=stores,
    )
