"""
Routing service — Step 5 of the pipeline.

Pure logic, NO LLM. Routes leads based on configurable score thresholds:
- score >= ROUTING_HIGH_THRESHOLD  → SALES_QUEUE
- score >= ROUTING_MEDIUM_THRESHOLD → NURTURE_QUEUE
- score < ROUTING_MEDIUM_THRESHOLD  → ARCHIVE

Thresholds are configurable via environment variables.
"""

import time
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.execution_log import ExecutionLog
from app.models.lead import Lead
from app.models.routing import RoutingDecision


def route_lead(session: Session, lead: Lead, lead_score: int) -> str:
    """Route a scored lead to the appropriate queue.

    Deterministic: same score always routes to the same queue.
    Thresholds are configurable via environment variables.

    Args:
        session: Sync SQLAlchemy session (Celery worker).
        lead: Lead ORM instance with status=SCORED.
        lead_score: The lead's calculated score (0-100).

    Returns:
        str: Queue name (SALES_QUEUE, NURTURE_QUEUE, or ARCHIVE).

    Side effects:
        - Creates RoutingDecision record in DB
        - Creates ExecutionLog record in DB
        - Updates lead status to ROUTED then COMPLETE
    """
    lead_id = str(lead.id)
    log = logger.bind(lead_id=lead_id, stage="routing")
    start_time = time.time()

    # Log execution start
    exec_log = ExecutionLog(
        lead_id=lead.id,
        stage="routing",
        status="STARTED",
        attempt_number=1,
    )
    session.add(exec_log)
    session.flush()

    try:
        # Determine queue based on configurable thresholds
        if lead_score >= settings.ROUTING_HIGH_THRESHOLD:
            queue = "SALES_QUEUE"
            reason = (
                f"Score {lead_score} >= {settings.ROUTING_HIGH_THRESHOLD} (high threshold). "
                f"Lead qualifies for immediate sales follow-up."
            )
        elif lead_score >= settings.ROUTING_MEDIUM_THRESHOLD:
            queue = "NURTURE_QUEUE"
            reason = (
                f"Score {lead_score} >= {settings.ROUTING_MEDIUM_THRESHOLD} (medium threshold) "
                f"but < {settings.ROUTING_HIGH_THRESHOLD} (high threshold). "
                f"Lead qualifies for nurture campaign."
            )
        else:
            queue = "ARCHIVE"
            reason = (
                f"Score {lead_score} < {settings.ROUTING_MEDIUM_THRESHOLD} (medium threshold). "
                f"Lead does not meet minimum qualification criteria."
            )

        duration_ms = int((time.time() - start_time) * 1000)

        # Persist routing decision
        routing_decision = RoutingDecision(
            lead_id=lead.id,
            queue=queue,
            routing_reason=reason,
            score_at_routing=lead_score,
        )
        session.add(routing_decision)

        # Update execution log
        exec_log.status = "SUCCESS"
        exec_log.duration_ms = duration_ms

        # Update lead status to COMPLETE
        lead.status = "COMPLETE"
        lead.updated_at = datetime.now(timezone.utc)

        session.flush()

        log.info(
            "Routing completed",
            queue=queue,
            score=lead_score,
            high_threshold=settings.ROUTING_HIGH_THRESHOLD,
            medium_threshold=settings.ROUTING_MEDIUM_THRESHOLD,
            duration_ms=duration_ms,
        )

        return queue

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        log.error("Routing failed", error=str(e), error_type=type(e).__name__)

        exec_log.status = "FAILED"
        exec_log.duration_ms = duration_ms
        exec_log.error_message = str(e)
        session.flush()

        raise
