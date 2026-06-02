FROM python:3.11-slim

# Install system dependencies required for OpenCV and media processing
RUN apt-get update && apt-get install -y \
    libgl1 \
    libglib2.0-0 \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Set environment variables
ENV PYTHONPATH=/app
ENV SI_DB_PATH=/app/data/store_intelligence.db
ENV SI_POS_CSV_PATH="/app/dataset/Brigade_Bangalore_10_April_26 (1)bc6219c.csv"

# Expose API port
EXPOSE 8000

# Run the FastAPI application
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
