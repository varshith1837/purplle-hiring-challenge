"""
Event ingestion endpoint for the Store Intelligence API.
Handles batch event ingestion with validation, deduplication, and partial failure.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.database import db
from app.models import EventIn, IngestError, IngestResponse

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["ingestion"])


@router.post(
    "/events/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_200_OK,
    summary="Ingest a batch of events",
    responses={
        200: {"description": "All events accepted"},
        207: {"description": "Partial success — some events rejected"},
        400: {"description": "Malformed request body"},
    },
)
async def ingest_events(request: Request) -> JSONResponse:
    """
    Accept a batch of up to 500 events.
    - Validates each event individually against the EventIn schema.
    - Deduplicates by event_id (idempotent).
    - On partial failure: accepts valid events and reports errors for invalid ones.
    """
    # --- Parse raw body ---
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        logger.warning("ingest.malformed_json")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": "Malformed JSON body",
                "accepted": 0,
                "rejected": 0,
                "duplicates": 0,
                "errors": [],
            },
        )

    raw_events = body.get("events")
    if raw_events is None or not isinstance(raw_events, list):
        logger.warning("ingest.missing_events_field")
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": "'events' field is required and must be a list",
                "accepted": 0,
                "rejected": 0,
                "duplicates": 0,
                "errors": [],
            },
        )

    if len(raw_events) > 500:
        logger.warning("ingest.batch_too_large", count=len(raw_events))
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "detail": f"Batch size {len(raw_events)} exceeds maximum of 500",
                "accepted": 0,
                "rejected": 0,
                "duplicates": 0,
                "errors": [],
            },
        )

    # --- Validate each event individually ---
    valid_events: list[dict[str, Any]] = []
    errors: list[IngestError] = []

    for idx, raw in enumerate(raw_events):
        try:
            event = EventIn.model_validate(raw)
            valid_events.append(event.model_dump())
        except ValidationError as exc:
            error_msg = "; ".join(
                f"{'.'.join(str(l) for l in e['loc'])}: {e['msg']}"
                for e in exc.errors()
            )
            errors.append(
                IngestError(
                    index=idx,
                    event_id=raw.get("event_id") if isinstance(raw, dict) else None,
                    error=error_msg,
                )
            )
        except Exception as exc:
            errors.append(
                IngestError(
                    index=idx,
                    event_id=raw.get("event_id") if isinstance(raw, dict) else None,
                    error=str(exc),
                )
            )

    # --- Insert valid events into database ---
    accepted = 0
    duplicates = 0
    db_errors: list[IngestError] = []

    if valid_events:
        try:
            accepted, duplicates, db_errs = await db.insert_events_batch(valid_events)
            for db_err in db_errs:
                db_errors.append(
                    IngestError(
                        index=db_err["index"],
                        event_id=db_err.get("event_id"),
                        error=db_err["error"],
                    )
                )
        except Exception as exc:
            logger.error("ingest.db_insert_failed", error=str(exc))
            # All valid events become errors
            for i, ev in enumerate(valid_events):
                db_errors.append(
                    IngestError(
                        index=i,
                        event_id=ev.get("event_id"),
                        error=f"Database error: {exc}",
                    )
                )

    all_errors = errors + db_errors
    rejected = len(all_errors)

    resp = IngestResponse(
        accepted=accepted,
        rejected=rejected,
        duplicates=duplicates,
        errors=all_errors,
    )

    # Choose status code
    if rejected > 0 and accepted > 0:
        http_status = status.HTTP_207_MULTI_STATUS
    elif rejected > 0 and accepted == 0 and duplicates == 0:
        http_status = status.HTTP_400_BAD_REQUEST
    else:
        http_status = status.HTTP_200_OK

    logger.info(
        "ingest.complete",
        accepted=accepted,
        rejected=rejected,
        duplicates=duplicates,
        total=len(raw_events),
    )

    return JSONResponse(
        status_code=http_status,
        content=resp.model_dump(),
    )
