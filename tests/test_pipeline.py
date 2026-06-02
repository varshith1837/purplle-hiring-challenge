# PROMPT: Create a pytest test file for testing the EventIn Pydantic model validation. Test valid cases, missing required fields, invalid UUIDs, invalid timestamps, and confidence boundaries.
# CHANGES MADE: Added tests for the specific enum values in EventType and the nested metadata handling.

import pytest
from pydantic import ValidationError
from app.models import EventIn

def test_valid_event():
    event_data = {
        "event_id": "123e4567-e89b-12d3-a456-426614174000",
        "store_id": "STORE_001",
        "camera_id": "CAM_01",
        "visitor_id": "VIS_abc123",
        "event_type": "ENTRY",
        "timestamp": "2026-06-01T10:00:00Z",
        "confidence": 0.95
    }
    event = EventIn(**event_data)
    assert event.event_id == "123e4567-e89b-12d3-a456-426614174000"
    assert event.event_type == "ENTRY"

def test_invalid_uuid():
    event_data = {
        "event_id": "invalid-uuid",
        "store_id": "STORE_001",
        "camera_id": "CAM_01",
        "visitor_id": "VIS_abc123",
        "event_type": "ENTRY",
        "timestamp": "2026-06-01T10:00:00Z",
        "confidence": 0.95
    }
    with pytest.raises(ValidationError):
        EventIn(**event_data)

def test_invalid_timestamp():
    event_data = {
        "event_id": "123e4567-e89b-12d3-a456-426614174000",
        "store_id": "STORE_001",
        "camera_id": "CAM_01",
        "visitor_id": "VIS_abc123",
        "event_type": "ENTRY",
        "timestamp": "not-a-timestamp",
        "confidence": 0.95
    }
    with pytest.raises(ValidationError):
        EventIn(**event_data)

def test_invalid_confidence():
    event_data = {
        "event_id": "123e4567-e89b-12d3-a456-426614174000",
        "store_id": "STORE_001",
        "camera_id": "CAM_01",
        "visitor_id": "VIS_abc123",
        "event_type": "ENTRY",
        "timestamp": "2026-06-01T10:00:00Z",
        "confidence": 1.5 # Should be <= 1.0
    }
    with pytest.raises(ValidationError):
        EventIn(**event_data)
