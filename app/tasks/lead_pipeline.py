"""
Lead pipeline Celery task — LangGraph-orchestrated processing.

Architecture (two concerns, two tools):
    Celery handles: task distribution, retry with backoff, acks_late,
                   dead-lettering, worker crash recovery
    LangGraph handles: stage orchestration, conditional routing,
                      idempotent resume via graph edges

The Celery task is now a thin wrapper:
1. Fetch lead from DB
2. Build LangGraph pipeline
3. Invoke the graph
4. Handle errors (retry or dead-letter)

All business logic lives in the graph nodes, which call the
existing service functions (enrichment, scoring, routing).
"""

import traceback
from datetime import datetime, timezone

from celery import current_task
from loguru import logger
from sqlalchemy import select

from app.core.database import get_sync_session_ctx
from app.models.lead import Lead
from app.tasks.celery_app import celery_app
from app.tasks.retry_policies import LEAD_PIPELINE_RETRY_POLICY
from app.api.routes.stream import publish_pipeline_event


# Pipeline stage order
STAGE_ORDER = ["RECEIVED", "VALIDATED", "ENRICHED", "SCORED", "ROUTED", "COMPLETE"]


@celery_app.task(
    bind=True,
    name="app.tasks.lead_pipeline.process_lead",
    max_retries=LEAD_PIPELINE_RETRY_POLICY["max_retries"],
    default_retry_delay=LEAD_PIPELINE_RETRY_POLICY["default_retry_delay"],
    retry_backoff=LEAD_PIPELINE_RETRY_POLICY["retry_backoff"],
    retry_jitter=LEAD_PIPELINE_RETRY_POLICY["retry_jitter"],
    acks_late=True,
    reject_on_worker_lost=True,
    track_started=True,
)
def process_lead(self, lead_id: str) -> dict:
    """Process a lead through the full pipeline using LangGraph.

    The graph handles stage orchestration and idempotent resume.
    This task handles distribution, retry, and error recovery.

    Args:
        lead_id: UUID string of the lead to process.

    Returns:
        dict: Processing result with lead_id, status, queue, score.
    """
    log = logger.bind(lead_id=lead_id, task_id=self.request.id)
    log.info("Pipeline task started", attempt=self.request.retries + 1)

    publish_pipeline_event(lead_id, "pipeline", "STARTED", {
        "attempt": self.request.retries + 1,
    })

    session = get_sync_session_ctx()

    try:
        # Fetch lead
        lead = session.execute(
            select(Lead).where(Lead.id == lead_id)
        )
        lead = lead.scalar_one_or_none()

        if lead is None:
            log.error("Lead not found")
            return {"lead_id": lead_id, "status": "NOT_FOUND", "error": "Lead not found"}

        # Check if already complete
        if lead.status in ("COMPLETE", "FAILED", "REJECTED"):
            log.info("Lead already processed", status=lead.status)
            return {"lead_id": lead_id, "status": lead.status, "message": "Already processed"}

        # --- Execute LangGraph Pipeline ---
        from app.services.langgraph_pipeline import build_pipeline_graph

        domain = lead.email.split("@")[-1] if "@" in lead.email else "unknown.com"

        graph = build_pipeline_graph()
        result_state = graph.invoke({
            "lead_id": lead_id,
            "session": session,
            "lead": lead,
            "message": lead.message,
            "domain": domain,
            "checkpoint": lead.pipeline_checkpoint or {},
            "current_status": lead.status,
            "intent_result": None,
            "research_result": None,
            "categorization_result": None,
            "enrichment_result": None,
            "scoring_result": None,
            "queue": None,
            "error": None,
            "retry_count": self.request.retries,
        })

        queue = result_state.get("queue", "UNKNOWN")
        score = (
            result_state["scoring_result"].lead_score
            if result_state.get("scoring_result")
            else None
        )

        log.info("Pipeline completed successfully", final_status=lead.status, queue=queue)

        publish_pipeline_event(lead_id, "pipeline", "COMPLETE", {
            "queue": queue,
            "score": score,
        })

        return {
            "lead_id": lead_id,
            "status": "COMPLETE",
            "queue": queue,
            "score": score,
        }

    except Exception as e:
        log.error(
            "Pipeline failed",
            error=str(e),
            error_type=type(e).__name__,
            attempt=self.request.retries + 1,
            max_retries=self.max_retries,
        )

        publish_pipeline_event(lead_id, "pipeline", "FAILED", {
            "error": str(e),
            "attempt": self.request.retries + 1,
        })

        # Try to update lead status in DB
        try:
            lead_obj = session.execute(
                select(Lead).where(Lead.id == lead_id)
            )
            lead_obj = lead_obj.scalar_one_or_none()
            if lead_obj:
                if self.request.retries >= self.max_retries:
                    # Max retries reached — dead-letter
                    lead_obj.status = "FAILED"
                    lead_obj.failure_reason = f"{type(e).__name__}: {str(e)}"
                    lead_obj.flag_for_review = True
                    lead_obj.flag_reason = (
                        f"Pipeline failed after {self.request.retries + 1} attempts: {str(e)}"
                    )
                    lead_obj.dead_lettered_at = datetime.now(timezone.utc)
                    lead_obj.updated_at = datetime.now(timezone.utc)
                    session.commit()
                    log.error("Lead dead-lettered after max retries", lead_id=lead_id)

                    publish_pipeline_event(lead_id, "pipeline", "DEAD_LETTERED", {
                        "error": str(e),
                        "total_attempts": self.request.retries + 1,
                    })

                    return {
                        "lead_id": lead_id,
                        "status": "FAILED",
                        "error": str(e),
                        "dead_lettered": True,
                    }
            session.commit()
        except Exception as db_error:
            log.error("Failed to update lead status on error", db_error=str(db_error))
            session.rollback()

        # Retry if attempts remain
        try:
            raise self.retry(exc=e)
        except self.MaxRetriesExceededError:
            log.error("Max retries exceeded, task will not be retried")
            return {
                "lead_id": lead_id,
                "status": "FAILED",
                "error": str(e),
                "dead_lettered": True,
            }

    finally:
        session.close()
