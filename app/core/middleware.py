"""
Request correlation ID middleware.

Assigns a unique correlation_id to every incoming request and propagates it
through the entire pipeline: HTTP response headers → Celery task args →
execution logs → structured log output.

Why this matters:
- In production, when something fails at 3 AM, you need to trace a single
  request from API entry through every async stage to the final DB write.
- Without correlation IDs, you're grep-ing through logs by timestamp
  and hoping no two requests overlapped. That doesn't scale.

Usage:
    # In any route handler:
    correlation_id = request.state.correlation_id

    # In Celery tasks (passed as kwarg):
    correlation_id = kwargs.get("correlation_id", "unknown")
"""

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from loguru import logger


class CorrelationIDMiddleware(BaseHTTPMiddleware):
    """Inject a correlation ID into every request lifecycle.

    The ID is:
    1. Read from X-Correlation-ID header (if client provides one)
    2. Generated as UUID4 if not provided
    3. Attached to request.state for route handlers
    4. Returned in response headers for client-side tracing
    5. Bound to loguru context for all log lines in this request
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # Accept client-provided correlation ID or generate one
        correlation_id = request.headers.get(
            "X-Correlation-ID",
            str(uuid.uuid4()),
        )

        # Attach to request state (accessible in route handlers)
        request.state.correlation_id = correlation_id

        # Bind to loguru context — every log line in this request
        # will automatically include the correlation_id
        with logger.contextualize(correlation_id=correlation_id):
            response = await call_next(request)

        # Return in response headers for client-side tracing
        response.headers["X-Correlation-ID"] = correlation_id
        return response
