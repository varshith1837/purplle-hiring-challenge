"""
FastAPI entrypoint for the Store Intelligence API.
Mounts all routers, middleware, and lifecycle handlers.
"""

from __future__ import annotations

import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
import sqlite3
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.database import db

# Configure structlog
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(0),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger("store_intelligence")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown handlers."""
    # --- Startup ---
    logger.info("app.starting")
    await db.connect()

    # Load POS CSV
    pos_csv_path = os.getenv(
        "SI_POS_CSV_PATH",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "purplle_pos.csv"),
    )
    if os.path.isfile(pos_csv_path):
        count = await db.load_pos_csv(pos_csv_path)
        logger.info("app.pos_loaded", csv_path=pos_csv_path, transactions=count)
    else:
        logger.warning("app.pos_csv_not_found", csv_path=pos_csv_path)

    # Record startup time for health endpoint
    from app.health import set_startup_time
    set_startup_time()

    logger.info("app.started")

    yield

    # --- Shutdown ---
    logger.info("app.shutting_down")
    await db.close()
    logger.info("app.stopped")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Store Intelligence API",
    description="Real-time retail analytics API for the Purplle store intelligence system.",
    version="1.0.0",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS Middleware — allow all origins for dashboard
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Structured Logging Middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def logging_middleware(request: Request, call_next):
    """
    Structured JSON logging middleware.
    Logs: trace_id, store_id, endpoint, latency_ms, event_count, status_code.
    """
    trace_id = str(uuid.uuid4())
    request.state.trace_id = trace_id
    start_time = time.perf_counter()

    # Extract store_id from path if present
    store_id = None
    path_parts = request.url.path.strip("/").split("/")
    if "stores" in path_parts:
        idx = path_parts.index("stores")
        if idx + 1 < len(path_parts):
            store_id = path_parts[idx + 1]

    try:
        response = await call_next(request)
    except Exception:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        logger.error(
            "request.unhandled_exception",
            trace_id=trace_id,
            store_id=store_id,
            endpoint=request.url.path,
            method=request.method,
            latency_ms=latency_ms,
        )
        raise

    latency_ms = round((time.perf_counter() - start_time) * 1000, 2)

    # Attempt to get event_count from response (for ingest endpoint)
    event_count = None
    if request.url.path == "/events/ingest" and request.method == "POST":
        # event_count is logged by the endpoint itself; we note it was an ingest
        event_count = "see_endpoint_log"

    logger.info(
        "request.completed",
        trace_id=trace_id,
        store_id=store_id,
        endpoint=request.url.path,
        method=request.method,
        latency_ms=latency_ms,
        status_code=response.status_code,
        event_count=event_count,
    )

    response.headers["X-Trace-ID"] = trace_id
    return response


# ---------------------------------------------------------------------------
# Global Exception Handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Return structured JSON errors, never raw stack traces."""
    trace_id = getattr(request.state, "trace_id", "unknown")
    error_type = type(exc).__name__
    
    logger.error(
        "unhandled_exception",
        trace_id=trace_id,
        endpoint=request.url.path,
        error_type=error_type,
        error=str(exc),
    )
    
    # Graceful degradation for database connection issues
    if isinstance(exc, sqlite3.OperationalError) or "OperationalError" in error_type or "DatabaseError" in error_type:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "detail": "Database unavailable",
                "trace_id": trace_id,
            },
        )

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "detail": "Internal server error",
            "trace_id": trace_id,
        },
    )


# ---------------------------------------------------------------------------
# Mount Routers
# ---------------------------------------------------------------------------

from app.ingestion import router as ingestion_router  # noqa: E402
from app.metrics import router as metrics_router  # noqa: E402
from app.funnel import router as funnel_router  # noqa: E402
from app.anomalies import router as anomalies_router  # noqa: E402
from app.heatmap import router as heatmap_router  # noqa: E402
from app.health import router as health_router  # noqa: E402

app.include_router(ingestion_router)
app.include_router(metrics_router)
app.include_router(funnel_router)
app.include_router(anomalies_router)
app.include_router(heatmap_router)
app.include_router(health_router)

# ---------------------------------------------------------------------------
# Dashboard Static Files
# ---------------------------------------------------------------------------

dashboard_dir = Path(__file__).resolve().parent.parent / "dashboard"
if dashboard_dir.is_dir():
    app.mount(
        "/dashboard",
        StaticFiles(directory=str(dashboard_dir), html=True),
        name="dashboard",
    )
    logger.info("app.dashboard_mounted", path=str(dashboard_dir))


# ---------------------------------------------------------------------------
# Root
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
async def root():
    """API root — basic service info."""
    return {
        "service": "Store Intelligence API",
        "version": "1.0.0",
        "docs": "/docs",
        "dashboard": "/dashboard",
    }
