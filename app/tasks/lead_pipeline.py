"""
Lead pipeline Celery task — the main task chain.

Executes the full pipeline: enrichment → scoring → routing.
(Validation happens synchronously in the API handler.)

Key reliability features:
- Idempotent: checks last successful stage before re-executing
- State machine: persists status before each stage transition
- Dead-letter: flags for review after max retries
- Full audit trail via execution_logs
"""

import traceback
from datetime import datetime, timezone

from celery import current_task
from loguru import logger
from sqlalchemy import select

from app.core.database import get_sync_session_ctx
from app.models.lead import Lead
from app.models.enrichment import Enrichment as EnrichmentModel
from app.schemas.enrichment import EnrichmentResult
from app.services.enrichment import enrich_lead
from app.services.scoring import score_lead
from app.services.routing import route_lead
from app.tasks.celery_app import celery_app
from app.tasks.retry_policies import LEAD_PIPELINE_RETRY_POLICY


# Pipeline stage order — used for idempotent resume
STAGE_ORDER = ["RECEIVED", "VALIDATED", "ENRICHED", "SCORED", "ROUTED", "COMPLETE"]


def _get_current_stage_index(status: str) -> int:
    """Get the index of the current stage in the pipeline.

    Args:
        status: Current lead status.

    Returns:
        int: Index in STAGE_ORDER, or -1 if unknown.
    """
    try:
        return STAGE_ORDER.index(status)
    except ValueError:
        return -1


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
    """Process a lead through the full pipeline.

    Stages (idempotent — resumes from last successful stage):
    1. Enrichment (LLM call via Gemini)
    2. Scoring (deterministic Python)
    3. Routing (configurable thresholds)

    Args:
        lead_id: UUID string of the lead to process.

    Returns:
        dict: Processing result with lead_id, status, queue.

    Raises:
        Retries via Celery on transient failures.
    """
    log = logger.bind(lead_id=lead_id, task_id=self.request.id)
    log.info("Pipeline task started", attempt=self.request.retries + 1)

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

        current_stage_idx = _get_current_stage_index(lead.status)

        # --- Stage 1: Enrichment ---
        if current_stage_idx < STAGE_ORDER.index("ENRICHED"):
            log.info("Starting enrichment stage")
            enrichment_result = enrich_lead(session, lead)
            session.commit()
            log.info("Enrichment stage complete")
        else:
            # Already enriched — load existing enrichment
            log.info("Enrichment already complete, loading existing data")
            existing = session.execute(
                select(EnrichmentModel).where(EnrichmentModel.lead_id == lead.id)
            )
            enrichment_model = existing.scalar_one_or_none()
            if enrichment_model:
                enrichment_result = EnrichmentResult(
                    lead_category=enrichment_model.lead_category,
                    company_type=enrichment_model.company_type,
                    estimated_intent=enrichment_model.estimated_intent,
                    urgency_level=enrichment_model.urgency_level,
                    pain_points=enrichment_model.pain_points,
                    ai_summary=enrichment_model.ai_summary,
                )
            else:
                log.error("Enrichment record missing for ENRICHED lead")
                raise ValueError("Enrichment record missing")

        # --- Stage 2: Scoring ---
        if current_stage_idx < STAGE_ORDER.index("SCORED"):
            log.info("Starting scoring stage")
            scoring_result = score_lead(session, lead, enrichment_result)
            session.commit()
            log.info("Scoring stage complete", score=scoring_result.lead_score)
        else:
            log.info("Scoring already complete")
            scoring_result = None  # Not needed for routing lookup

        # --- Stage 3: Routing ---
        if current_stage_idx < STAGE_ORDER.index("ROUTED"):
            # Get score for routing
            if scoring_result:
                lead_score = scoring_result.lead_score
            else:
                # Load from DB if scoring was already done
                from app.models.score import Score as ScoreModel
                existing_score = session.execute(
                    select(ScoreModel).where(ScoreModel.lead_id == lead.id)
                )
                score_model = existing_score.scalar_one_or_none()
                lead_score = score_model.lead_score if score_model else 0

            log.info("Starting routing stage")
            queue = route_lead(session, lead, lead_score)
            session.commit()
            log.info("Routing stage complete", queue=queue)
        else:
            log.info("Routing already complete")
            queue = "ALREADY_ROUTED"

        log.info("Pipeline completed successfully", final_status=lead.status)

        return {
            "lead_id": lead_id,
            "status": "COMPLETE",
            "queue": queue,
            "score": scoring_result.lead_score if scoring_result else None,
        }

    except Exception as e:
        log.error(
            "Pipeline failed",
            error=str(e),
            error_type=type(e).__name__,
            attempt=self.request.retries + 1,
            max_retries=self.max_retries,
        )

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
