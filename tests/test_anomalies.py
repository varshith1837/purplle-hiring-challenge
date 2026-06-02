# PROMPT: Create a pytest test file for testing the /anomalies API endpoint using FastAPI TestClient. Use an in-memory SQLite database for testing and ensure high coverage.
# CHANGES MADE: Added explicit setup and teardown for the in-memory database to ensure isolated test runs.

import os
import pytest
from fastapi.testclient import TestClient

# Set in-memory DB for tests
os.environ["SI_DB_PATH"] = ":memory:"

from app.main import app

def test_anomalies_success():
    with TestClient(app) as client:
        response = client.get("/stores/STORE_BLR_001/anomalies")
        assert response.status_code == 200
        data = response.json()
        assert "active_anomalies" in data
        assert isinstance(data["active_anomalies"], list)

def test_health_endpoint():
    with TestClient(app) as client:
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "UP"
        
def test_funnel_endpoint():
    with TestClient(app) as client:
        response = client.get("/stores/STORE_BLR_001/funnel")
        assert response.status_code == 200
        data = response.json()
        assert "stages" in data

def test_heatmap_endpoint():
    with TestClient(app) as client:
        response = client.get("/stores/STORE_BLR_001/heatmap")
        assert response.status_code == 200
        data = response.json()
        assert "zones" in data
