# Store Intelligence Architecture Choices

This document outlines three key architectural choices made during the development of the Store Intelligence system, evaluating the alternatives considered, what AI suggested, and the final rationale.

## 1. Detection Model Selection

**Options Considered:**
- YOLOv8 (nano, small, medium)
- RT-DETR (Real-Time DEtection TRansformer)
- MediaPipe (Google's optimized CV framework)

**AI Suggestion:**
When prompted about the best model for retail CCTV tracking, AI initially suggested YOLOv8s (small) or YOLOv9 as a solid baseline, citing their strong performance on the COCO dataset for the "person" class and good balance of speed vs accuracy on modern hardware.

**My Choice & Rationale: YOLOv8n (Nano)**
While YOLOv8s is excellent, I chose YOLOv8n (Nano). The primary constraint in our environment is that we are running inference on a CPU, not a GPU. YOLOv8s has 11.2M parameters, whereas YOLOv8n has only 3.2M parameters. On a CPU, YOLOv8n runs significantly faster while still providing more than adequate accuracy for detecting full-body persons in 1080p retail footage. Furthermore, Ultralytics YOLOv8 has ByteTrack built-in natively, making the integration of detection and tracking seamless without needing external tracking libraries.

## 2. Event Schema Design

**Options Considered:**
- Deeply nested hierarchical JSON (e.g., separating camera data, store data, and visitor data into sub-objects).
- A flat schema with a generic metadata dictionary.
- Emitting raw bounding box coordinates for every frame.

**AI Suggestion:**
The AI suggested using a flat schema with a flexible `metadata` payload for event-specific data (like `queue_depth` or `sku_zone`). It advised against emitting per-frame bounding boxes as that would overwhelm the API with thousands of events per second, suggesting discrete state-change events instead.

**My Choice & Rationale: Flat Schema with Discrete Events**
I opted for the flat event schema with a `metadata` object (as implemented in `app/models.py`). 
Reasoning:
1. **Analytics Friendly**: Flat JSON structures map perfectly to columnar databases (like ClickHouse or BigQuery) and are much easier to query in SQL (or SQLite in our case) than deeply nested objects.
2. **Bandwidth Efficiency**: By emitting discrete events (`ZONE_ENTER`, `ZONE_DWELL`, `ZONE_EXIT`) rather than raw frame coordinates, we reduce network traffic by orders of magnitude, making the API scalable to 40+ stores.
3. **Extensibility**: The `metadata` dictionary allows us to add new fields (like `basket_value` or `demographics`) in the future without breaking the base schema or requiring API version bumps.

## 3. API Architecture: FastAPI + SQLite

**Options Considered:**
- Node.js / Express with MongoDB
- FastAPI with PostgreSQL
- FastAPI with SQLite (async)

**AI Suggestion:**
The AI noted that while PostgreSQL is the standard for production, SQLite is often sufficient for standalone analytics pipelines with moderate write loads, provided we use WAL (Write-Ahead Logging) or an async driver to avoid locking issues.

**My Choice & Rationale: FastAPI + SQLite (via aiosqlite)**
I chose FastAPI coupled with an asynchronous SQLite database (`aiosqlite`).
Reasoning:
1. **Simplicity and Containerization**: SQLite requires no separate database server. This means our `docker-compose.yml` only needs a single service block. For an evaluation/challenge environment, minimizing moving parts reduces the risk of deployment failure.
2. **Performance**: FastAPI's async nature handles high-concurrency ingestion well. By using `aiosqlite`, we ensure that database writes don't block the event loop, allowing the API to efficiently process batches of events from multiple cameras simultaneously.
3. **Pydantic Validation**: FastAPI's native integration with Pydantic ensures strict schema validation for the event stream at the boundary layer, meaning malformed events never reach the database.
