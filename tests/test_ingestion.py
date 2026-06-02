# PROMPT: Create a pytest test file for testing the /events/ingest API endpoint using FastAPI TestClient. Test idempotency, partial success, and missing fields.
# CHANGES MADE: Used TestClient and in-memory DB to ensure high coverage of ingestion.py and database.py logic.

import os
import pytest
from fastapi.testclient import TestClient

# Set in-memory DB for tests
os.environ["SI_DB_PATH"] = ":memory:"

from app.main import app

def test_ingest_success():
    with TestClient(app) as client:
        payload = {
            "events": [
                {
                    "event_id": "11111111-1111-1111-1111-111111111111",
                    "store_id": "STORE_001",
                    "camera_id": "CAM_01",
                    "visitor_id": "VIS_abc123",
                    "event_type": "ENTRY",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "confidence": 0.95
                }
            ]
        }
        response = client.post("/events/ingest", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["accepted"] == 1
        assert data["duplicates"] == 0
        assert data["rejected"] == 0

def test_ingest_idempotency():
    with TestClient(app) as client:
        payload = {
            "events": [
                {
                    "event_id": "22222222-2222-2222-2222-222222222222",
                    "store_id": "STORE_001",
                    "camera_id": "CAM_01",
                    "visitor_id": "VIS_abc123",
                    "event_type": "ENTRY",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "confidence": 0.95
                }
            ]
        }
        # First call
        client.post("/events/ingest", json=payload)
        # Second call
        response = client.post("/events/ingest", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["accepted"] == 0
        assert data["duplicates"] == 1
        assert data["rejected"] == 0

def test_ingest_partial_success():
    with TestClient(app) as client:
        payload = {
            "events": [
                {
                    "event_id": "33333333-3333-3333-3333-333333333333",
                    "store_id": "STORE_001",
                    "camera_id": "CAM_01",
                    "visitor_id": "VIS_abc123",
                    "event_type": "ENTRY",
                    "timestamp": "2026-06-01T10:00:00Z",
                    "confidence": 0.95
                },
                {
                    "event_id": "44444444-4444-4444-4444-444444444444",
                    "store_id": "STORE_001",
                    # missing camera_id and visitor_id and invalid confidence
                    "confidence": 2.0 
                }
            ]
        }
        response = client.post("/events/ingest", json=payload)
        assert response.status_code == 207
        data = response.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 1

def test_ingest_malformed():
    with TestClient(app) as client:
        response = client.post("/events/ingest", content="not-json")
        assert response.status_code == 400
        
        response = client.post("/events/ingest", json={"not_events": []})
        assert response.status_code == 400

        # batch too large
        payload = {"events": [{"event_id": f"{i:08d}-0000-0000-0000-000000000000"} for i in range(501)]}
        response = client.post("/events/ingest", json=payload)
        assert response.status_code == 400
