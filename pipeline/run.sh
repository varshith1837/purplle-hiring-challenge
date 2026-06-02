#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# pipeline/run.sh
# One command to process all CCTV clips through the detection pipeline.
#
# Usage:
#   bash pipeline/run.sh [INPUT_DIR] [OUTPUT_DIR] [API_URL]
#
# Defaults:
#   INPUT_DIR  = ../dataset/CCTV Footage
#   OUTPUT_DIR = ../output
#   API_URL    = http://localhost:8000
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

INPUT_DIR="${1:-$PROJECT_ROOT/dataset/CCTV Footage}"
OUTPUT_DIR="${2:-$PROJECT_ROOT/output}"
API_URL="${3:-http://localhost:8000}"

EVENTS_FILE="$OUTPUT_DIR/events.jsonl"

echo "=================================================================="
echo " Store Intelligence — CCTV Detection Pipeline"
echo "=================================================================="
echo " Project root : $PROJECT_ROOT"
echo " Input dir    : $INPUT_DIR"
echo " Output dir   : $OUTPUT_DIR"
echo " Events file  : $EVENTS_FILE"
echo " API URL      : $API_URL"
echo "=================================================================="

# Create output directory
mkdir -p "$OUTPUT_DIR"

# Clear previous events file
> "$EVENTS_FILE"

# Count video files
VIDEO_COUNT=$(find "$INPUT_DIR" -maxdepth 1 -name "*.mp4" -type f | wc -l)
if [ "$VIDEO_COUNT" -eq 0 ]; then
    echo "ERROR: No .mp4 files found in $INPUT_DIR" >&2
    exit 1
fi
echo "Found $VIDEO_COUNT video file(s)"
echo ""

# Run pipeline
cd "$PROJECT_ROOT"
python -m pipeline.detect \
    --input "$INPUT_DIR" \
    --output "$EVENTS_FILE" \
    --store-id STORE_BLR_001 \
    --api-url "$API_URL" \
    --skip-frames 3 \
    --conf-threshold 0.35

echo ""
echo "=================================================================="
echo " Pipeline complete!"
echo " Events written to: $EVENTS_FILE"
EVENT_COUNT=$(wc -l < "$EVENTS_FILE" 2>/dev/null || echo 0)
echo " Total events: $EVENT_COUNT"
echo "=================================================================="
