# Store Intelligence

An end-to-end retail store analytics system that processes raw CCTV footage, tracks customer behavior, and surfaces actionable insights via a real-time API and live dashboard. Built for the Purplle Tech Challenge 2026.

## Architecture

```text
CCTV Footage → Detection Pipeline (YOLOv8 + ByteTrack) → Events JSONL
                                                                 ↓
Live Dashboard ← Intelligence API (FastAPI + SQLite) ← (Ingest POST)
```

## Quick Start

Get the system running in 5 commands:

1. **Clone the repository** (if applicable):
   ```bash
   git clone <repo-url>
   cd store-intelligence
   ```

2. **Start the API and Dashboard**:
   ```bash
   docker compose up -d
   ```

3. **Install Pipeline Dependencies** (requires Python 3.11+):
   ```bash
   pip install -r requirements.txt
   ```

4. **Run the Detection Pipeline**:
   ```bash
   cd pipeline
   ./run.sh
   # On Windows: run.bat
   ```

5. **View the Live Dashboard**:
   Open [http://localhost:8000/dashboard](http://localhost:8000/dashboard) in your browser.

## How It Works

### The Detection Pipeline
The pipeline uses `YOLOv8n` (nano) for person detection to maximize CPU inference speed, coupled with `ByteTrack` for multi-object tracking. It processes video frames, categorizes zones based on bounding box coordinates mapped via `data/store_layout.json`, and identifies staff using color histogram analysis of the upper body. It emits structured JSON events.

### The Intelligence API
A production-ready FastAPI application backed by an asynchronous SQLite database. It ingests the high-throughput event stream, correlates it with real POS transaction data (`dataset/Brigade_Bangalore_10_April_26 (1)bc6219c.csv`), and calculates real-time metrics, funnels, heatmaps, and anomalies.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| POST | `/events/ingest` | Ingests a batch of events (up to 500) |
| GET | `/stores/{id}/metrics` | Returns unique visitors, conversion rate, etc. |
| GET | `/stores/{id}/funnel` | Returns the session-based conversion funnel |
| GET | `/stores/{id}/heatmap` | Returns zone visit frequency and dwell times |
| GET | `/stores/{id}/anomalies`| Returns active operational anomalies |
| GET | `/health` | Returns system health and feed freshness |

## Testing

Run the test suite using pytest:
```bash
pytest tests/ --cov=app --cov-report=term-missing
```

## Documentation

Detailed architectural decisions and AI usage logs can be found in the `docs/` directory:
- [DESIGN.md](docs/DESIGN.md) - Architecture overview and AI-assisted decisions.
- [CHOICES.md](docs/CHOICES.md) - Detailed rationale for 3 key architectural choices.
