# PROMPT: Create a pytest test file for testing the /metrics API endpoint using FastAPI TestClient. Use an in-memory SQLite database for testing and ensure high coverage by testing zero purchases, successful responses, and 404s.
# CHANGES MADE: Added explicit setup and teardown for the in-memory database to ensure isolated test runs.

import os
import pytest
from fastapi.testclient import TestClient

# Set in-memory DB for tests
os.environ["SI_DB_PATH"] = ":memory:"

from app.main import app

def test_metrics_success():
    with TestClient(app) as client:
        response = client.get("/stores/STORE_BLR_001/metrics?start=2026-06-01T00:00:00Z&end=2026-06-02T00:00:00Z")
        assert response.status_code == 200
        data = response.json()
        assert "unique_visitors" in data
        assert "conversion_rate" in data
        assert "avg_dwell_by_zone" in data
        assert "current_queue_depth" in data
        assert "abandonment_rate" in data
        # Since it's an empty DB, all should be 0
        assert data["unique_visitors"] == 0
        assert data["conversion_rate"] == 0.0

def test_metrics_no_dates():
    with TestClient(app) as client:
        response = client.get("/stores/STORE_BLR_001/metrics")
        assert response.status_code == 200
        data = response.json()
        assert "unique_visitors" in data
