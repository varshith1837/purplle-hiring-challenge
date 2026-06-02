# Store Intelligence Architecture Design

## Architecture Overview

The Store Intelligence system is composed of two major subsystems: the **Detection Pipeline** (Computer Vision) and the **Intelligence API** (Backend & Analytics). These are decoupled via a structured JSON-based event stream.

```text
+-------------------+       +-----------------------+       +-------------------+
|                   |       |                       |       |                   |
|  Raw CCTV Clips   +------->  Detection Pipeline   +------->   Event Stream    |
|  (CAM 1 - 5)      |       |  (YOLOv8 + ByteTrack) |       |  (events.jsonl)   |
|                   |       |                       |       |                   |
+-------------------+       +-----------+-----------+       +---------+---------+
                                        |                             |
                                        |                             v
                                        |                   +---------+---------+
                                        |                   |                   |
                                        +------------------->  Intelligence API |
                                          POST /ingest      |  (FastAPI+SQLite) |
                                                            |                   |
                                                            +---------+---------+
                                                                      |
                                                                      v
                                                            +---------+---------+
                                                            |                   |
                                                            |  Live Dashboard   |
                                                            |  (HTML/JS/CSS)    |
                                                            |                   |
                                                            +-------------------+
```

### Data Flow
1. **Video Processing**: `detect.py` processes each camera clip. It runs YOLOv8 for person detection and ByteTrack for associating detections across frames (tracking).
2. **Behavior Analysis**: For each tracked person, `zones.py` computes which store zone they are in based on bounding box coordinates and camera-to-zone mappings. `staff_classifier.py` identifies if the person is a staff member.
3. **Event Emission**: `emit.py` translates behaviors (entering, exiting, dwelling) into structured JSON events conforming to the API schema and POSTs them to the API.
4. **Data Ingestion**: The FastAPI application receives events at `POST /events/ingest`, validates them, and stores them in SQLite.
5. **Analytics Engine**: When dashboard or users query the API (e.g., `GET /metrics`, `GET /funnel`), the system computes real-time metrics by querying the SQLite event store and correlating with loaded POS transactions.

## AI-Assisted Decisions

1. **Detection Model Selection**: I used AI to evaluate tradeoffs between YOLOv8n, RT-DETR, and MediaPipe for this specific use case. The AI correctly pointed out that since we needed to run on CPU without a GPU available, YOLOv8n (nano) offers the best balance of inference speed and accuracy for person detection, whereas RT-DETR would be too slow on CPU. I agreed and implemented YOLOv8n.
2. **Tracking Algorithm**: When designing the tracking system, AI suggested using ByteTrack over DeepSORT. I agreed with this decision because ByteTrack associates every detection box (even low confidence ones) instead of throwing them away, handling occlusions better. Furthermore, ByteTrack doesn't require a heavy appearance feature extraction model (re-ID network) to run on every frame, which is critical for our CPU-bound environment.
3. **Staff Detection Heuristic**: Initially, I prompted the AI for a way to detect staff. It suggested using a color histogram approach on the upper body. I implemented this, but added a secondary fallback heuristic suggested by the AI: tracking dwell time. Since staff members typically stay in the camera view for much longer durations than customers, extreme track durations can serve as a strong secondary signal for staff classification.

## Zone Classification Approach

Zone classification relies on the `store_layout.json` configuration. 
- **Entry/Exit**: The entry camera has a defined horizontal line (y-fraction). If a track's centroid crosses this line from top to bottom, it's an `ENTRY`. Bottom to top is an `EXIT`.
- **Floor Zones**: For main floor cameras, the field of view is subdivided into logical regions (left, center, right) mapped to specific product zones (Skincare, Makeup, etc.).
- **Billing Zone**: The billing camera uses a specific bounding box fraction to define the "Queue Region". A person's bounding box center falling inside this region triggers a `BILLING_QUEUE_JOIN` event.

## Edge Case Handling

- **Partial Occlusions**: Handled gracefully by ByteTrack, which maintains tracks even when detection confidence drops temporarily.
- **Group Entry**: YOLOv8 naturally detects multiple distinct persons in a frame. The tracking assigns unique IDs to each, generating multiple independent `ENTRY` events.
- **Empty Store**: The API handles zero-event scenarios without dividing by zero, returning `0` or `0.0` for rates and counts.
