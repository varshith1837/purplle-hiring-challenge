"""
Pydantic models for the Store Intelligence API.
Defines the event schema, API request/response models.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Event Types
# ---------------------------------------------------------------------------

class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class AnomalyType(str, Enum):
    BILLING_QUEUE_SPIKE = "BILLING_QUEUE_SPIKE"
    CONVERSION_DROP = "CONVERSION_DROP"
    DEAD_ZONE = "DEAD_ZONE"


class Severity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Event Metadata
# ---------------------------------------------------------------------------

class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: Optional[int] = None


# ---------------------------------------------------------------------------
# Core Event Model
# ---------------------------------------------------------------------------

class EventIn(BaseModel):
    """Incoming event from the detection pipeline."""
    event_id: str = Field(..., description="UUID-v4, globally unique")
    store_id: str = Field(..., description="Store identifier from store_layout.json")
    camera_id: str = Field(..., description="Camera that produced this event")
    visitor_id: str = Field(..., description="Re-ID token, unique per visit session")
    event_type: EventType = Field(..., description="Event type from catalogue")
    timestamp: str = Field(..., description="ISO-8601 UTC timestamp")
    zone_id: Optional[str] = Field(None, description="Zone ID; null for ENTRY/EXIT")
    dwell_ms: int = Field(0, ge=0, description="Dwell duration in ms; 0 for instantaneous")
    is_staff: bool = Field(False, description="True if detected as staff")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Detection confidence")
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("event_id")
    @classmethod
    def validate_event_id(cls, v: str) -> str:
        try:
            uuid.UUID(v, version=4)
        except ValueError:
            raise ValueError("event_id must be a valid UUID-v4")
        return v

    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise ValueError("timestamp must be ISO-8601 format")
        return v


# ---------------------------------------------------------------------------
# Ingest Request / Response
# ---------------------------------------------------------------------------

class IngestRequest(BaseModel):
    events: list[EventIn] = Field(..., max_length=500)


class IngestError(BaseModel):
    index: int
    event_id: Optional[str] = None
    error: str


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    duplicates: int
    errors: list[IngestError] = []


# ---------------------------------------------------------------------------
# Metrics Response
# ---------------------------------------------------------------------------

class ZoneDwell(BaseModel):
    zone_id: str
    avg_dwell_ms: float
    visit_count: int


class MetricsResponse(BaseModel):
    store_id: str
    period_start: str
    period_end: str
    unique_visitors: int
    conversion_rate: float = Field(..., ge=0.0, le=1.0)
    avg_dwell_by_zone: list[ZoneDwell]
    current_queue_depth: int
    abandonment_rate: float = Field(..., ge=0.0, le=1.0)
    total_transactions: int


# ---------------------------------------------------------------------------
# Funnel Response
# ---------------------------------------------------------------------------

class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float = Field(..., ge=0.0, le=100.0)


class FunnelResponse(BaseModel):
    store_id: str
    period_start: str
    period_end: str
    stages: list[FunnelStage]
    total_sessions: int


# ---------------------------------------------------------------------------
# Heatmap Response
# ---------------------------------------------------------------------------

class ZoneHeat(BaseModel):
    zone_id: str
    zone_name: str
    visit_count: int
    avg_dwell_ms: float
    intensity: float = Field(..., ge=0.0, le=100.0, description="Normalised 0–100")
    data_confidence: str = Field("HIGH", description="LOW if <20 sessions")


class HeatmapResponse(BaseModel):
    store_id: str
    period_start: str
    period_end: str
    zones: list[ZoneHeat]


# ---------------------------------------------------------------------------
# Anomaly Response
# ---------------------------------------------------------------------------

class Anomaly(BaseModel):
    anomaly_type: AnomalyType
    severity: Severity
    zone_id: Optional[str] = None
    detail: str
    suggested_action: str
    detected_at: str


class AnomaliesResponse(BaseModel):
    store_id: str
    active_anomalies: list[Anomaly]


# ---------------------------------------------------------------------------
# Health Response
# ---------------------------------------------------------------------------

class StoreHealth(BaseModel):
    store_id: str
    last_event_at: Optional[str] = None
    event_count: int = 0
    status: str = "OK"
    warnings: list[str] = []


class HealthResponse(BaseModel):
    status: str = "UP"
    uptime_seconds: float
    database: str = "OK"
    stores: list[StoreHealth] = []
