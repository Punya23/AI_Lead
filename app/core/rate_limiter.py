"""
Rate limiter configuration.

Uses slowapi to enforce per-IP request limits on intake endpoints.
This prevents:
- API abuse from bots or scrapers
- Accidental DDoS from misconfigured integrations
- Resource exhaustion on the async pipeline

Design decisions:
- Rate limit by IP address (not API key — no auth layer in this version)
- Custom 429 response includes correlation_id for debugging
- Limit only applies to write endpoints (POST), not reads (GET)
"""

from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.core.config import settings

# Rate limiter instance — imported by route handlers
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[f"{settings.RATE_LIMIT_PER_MINUTE}/minute"],
    storage_uri=settings.REDIS_URL,
)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Custom 429 response — structured JSON, not plain text.

    Includes correlation_id so the client can reference this
    when asking "why was I rate limited?"
    """
    correlation_id = getattr(request.state, "correlation_id", "unknown")
    return JSONResponse(
        status_code=429,
        content={
            "error": "Rate limit exceeded",
            "detail": str(exc.detail),
            "correlation_id": correlation_id,
            "retry_after_seconds": 60,
        },
        headers={
            "Retry-After": "60",
            "X-Correlation-ID": correlation_id,
        },
    )
