"""
Server-Sent Events (SSE) endpoint for real-time pipeline monitoring.

Architecture:
    Celery worker  →  Redis Pub/Sub (channel: pipeline:events)  →  SSE endpoint  →  Client

Why SSE over WebSocket:
- SSE is unidirectional (server → client) — perfect for log streaming
- Built-in browser reconnection
- Simpler protocol — no handshake, no framing
- Works through proxies and load balancers without special config

Why Redis Pub/Sub:
- We already have Redis (Celery broker)
- Zero additional infrastructure
- Fan-out: multiple SSE clients all receive the same events
"""

import asyncio
import json
from datetime import datetime, timezone

import redis.asyncio as aioredis
from fastapi import APIRouter
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.core.config import settings

router = APIRouter(prefix="/api/v1", tags=["Streaming"])

# Redis channel name for pipeline events
PIPELINE_EVENTS_CHANNEL = "pipeline:events"


async def _event_generator():
    """Subscribe to Redis Pub/Sub and yield SSE events.

    Auto-disconnects after 5 minutes to prevent resource leaks.
    Client can reconnect (SSE has built-in reconnection).
    """
    r = aioredis.from_url(settings.REDIS_URL, decode_responses=True)
    pubsub = r.pubsub()
    await pubsub.subscribe(PIPELINE_EVENTS_CHANNEL)

    max_duration_seconds = 300  # 5 minutes
    start = asyncio.get_event_loop().time()

    try:
        while (asyncio.get_event_loop().time() - start) < max_duration_seconds:
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=1.0,
            )
            if message and message["type"] == "message":
                yield {
                    "event": "pipeline_event",
                    "data": message["data"],
                }
            else:
                # Send heartbeat every second to keep connection alive
                yield {
                    "event": "heartbeat",
                    "data": json.dumps({"timestamp": datetime.now(timezone.utc).isoformat()}),
                }
            await asyncio.sleep(0.5)

        # Connection timeout — send close event
        yield {
            "event": "timeout",
            "data": json.dumps({"message": "Stream timeout after 5 minutes. Reconnect to continue."}),
        }
    except asyncio.CancelledError:
        logger.debug("SSE client disconnected")
    finally:
        await pubsub.unsubscribe(PIPELINE_EVENTS_CHANNEL)
        await pubsub.aclose()
        await r.aclose()


@router.get("/stream/pipeline")
async def stream_pipeline_events():
    """Stream real-time pipeline processing events via SSE.

    Events include:
    - pipeline_event: Lead processing updates (stage transitions, scores, routing)
    - heartbeat: Keep-alive signal (every ~1 second)
    - timeout: Auto-disconnect after 5 minutes

    Usage:
        curl -N http://localhost:8000/api/v1/stream/pipeline

    Or in JavaScript:
        const es = new EventSource('/api/v1/stream/pipeline');
        es.addEventListener('pipeline_event', (e) => console.log(JSON.parse(e.data)));
    """
    return EventSourceResponse(_event_generator())


def publish_pipeline_event(
    lead_id: str,
    stage: str,
    status: str,
    data: dict | None = None,
) -> None:
    """Publish a pipeline event to Redis Pub/Sub (called from Celery worker).

    This is a sync function — safe to call from Celery tasks.

    Args:
        lead_id: UUID of the lead being processed.
        stage: Pipeline stage (enrichment, scoring, routing).
        status: Stage status (STARTED, SUCCESS, FAILED).
        data: Optional additional data (score, queue, error, etc.)
    """
    try:
        import redis as sync_redis
        r = sync_redis.from_url(settings.REDIS_URL)
        event = json.dumps({
            "lead_id": lead_id,
            "stage": stage,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": data or {},
        })
        r.publish(PIPELINE_EVENTS_CHANNEL, event)
        r.close()
    except Exception as e:
        # Never crash the pipeline for a streaming event
        logger.debug(f"Failed to publish pipeline event: {e}")
