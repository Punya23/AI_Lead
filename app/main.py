"""
FastAPI application entry point.

Design priorities (in order):
1. Reliability — structured errors, correlation IDs, never leak tracebacks
2. Observability — every request traceable from intake to routing
3. Resilience — graceful degradation, not hard failures
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from app.core.config import settings
from app.core.logging import setup_logging
from app.core.middleware import CorrelationIDMiddleware
from app.core.rate_limiter import limiter, rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.api.routes.health import router as health_router
from app.api.routes.leads import router as leads_router
from app.api.routes.webhooks import router as webhooks_router
from app.api.routes.admin import router as admin_router
from app.api.routes.stream import router as stream_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifecycle manager.

    Startup: initialize structured logging.
    Shutdown: cleanup (connection pools handled by SQLAlchemy).
    """
    setup_logging()
    logger.info(
        "Application started",
        app_name=settings.APP_NAME,
        env=settings.APP_ENV,
        debug=settings.DEBUG,
    )
    yield
    logger.info("Application shutting down")


app = FastAPI(
    title="Geta.ai Lead Processing Pipeline",
    description=(
        "AI-powered inbound lead processing and enrichment workflow. "
        "Accepts leads via REST API or CSV upload, validates, enriches with AI, "
        "scores deterministically, and routes to the appropriate queue."
    ),
    version="1.0.0",
    lifespan=lifespan,
)


# --- Middleware (order matters: last added = first executed) ---

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rate limiter state (required by slowapi)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

# Correlation ID — must be outermost to capture all requests
app.add_middleware(CorrelationIDMiddleware)


# --- Global Exception Handler ---
# Never leak stack traces to clients. Always return structured JSON
# with a correlation_id for debugging.

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all for unhandled exceptions.

    Returns a structured JSON error with the correlation_id so
    the operator can grep logs for the full traceback.
    In production, the client never sees internal details.
    """
    correlation_id = getattr(request.state, "correlation_id", "unknown")
    logger.exception(
        "Unhandled exception",
        correlation_id=correlation_id,
        path=str(request.url.path),
        method=request.method,
        error_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "correlation_id": correlation_id,
            "message": (
                str(exc) if settings.DEBUG
                else "An unexpected error occurred. Use the correlation_id to trace this in logs."
            ),
        },
        headers={"X-Correlation-ID": correlation_id},
    )


# --- Route Registration ---
app.include_router(health_router)
app.include_router(leads_router)
app.include_router(webhooks_router)
app.include_router(admin_router)
app.include_router(stream_router)

# --- Static Files & Dashboard ---
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/dashboard", tags=["Dashboard"], include_in_schema=False)
async def dashboard():
    """Serve the visual dashboard UI."""
    html_path = STATIC_DIR / "dashboard.html"
    if html_path.exists():
        return FileResponse(str(html_path), media_type="text/html")
    return JSONResponse({"error": "Dashboard not found"}, status_code=404)


@app.get("/", tags=["Root"])
async def root():
    """Root endpoint — API information."""
    return {
        "name": settings.APP_NAME,
        "version": "1.0.0",
        "description": "AI-Powered Lead Processing Pipeline",
        "docs": "/docs",
        "health": "/health",
        "dashboard": "/dashboard",
    }
