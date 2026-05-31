"""
Enrichment service — Step 3 of the pipeline.

Orchestrates the AI enrichment process:
1. Calls the LLM client to get structured enrichment data
2. Handles fallback on LLM failure
3. Persists the enrichment result to the database
4. Logs execution details for the audit trail
"""

import time
import traceback
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session

from app.core.exceptions import LLMError
from app.models.enrichment import Enrichment
from app.models.execution_log import ExecutionLog
from app.models.lead import Lead
from app.schemas.enrichment import EnrichmentResult
from app.services.llm_client import call_enrichment_llm, get_fallback_enrichment


def enrich_lead(session: Session, lead: Lead) -> EnrichmentResult:
    """Run AI enrichment on a validated lead.

    Calls Gemini to extract category, intent, urgency, pain points,
    and summary. On failure after retries, uses fallback defaults
    and flags the lead for manual review.

    Args:
        session: Sync SQLAlchemy session (used in Celery worker).
        lead: Lead ORM instance with status=VALIDATED.

    Returns:
        EnrichmentResult: Validated enrichment data.

    Side effects:
        - Creates Enrichment record in DB
        - Creates ExecutionLog record in DB
        - Updates lead status to ENRICHED (or flags for review on failure)
    """
    lead_id = str(lead.id)
    log = logger.bind(lead_id=lead_id, stage="enrichment")
    start_time = time.time()

    # Log execution start
    exec_log = ExecutionLog(
        lead_id=lead.id,
        stage="enrichment",
        status="STARTED",
        attempt_number=1,
    )
    session.add(exec_log)
    session.flush()

    try:
        # Call LLM
        enrichment_result, raw_response = call_enrichment_llm(
            name=lead.name,
            email=lead.email,
            company=lead.company,
            message=lead.message,
            source=lead.source,
            lead_id=lead_id,
        )

        duration_ms = int((time.time() - start_time) * 1000)

        # Persist enrichment
        enrichment = Enrichment(
            lead_id=lead.id,
            lead_category=enrichment_result.lead_category,
            company_type=enrichment_result.company_type,
            estimated_intent=enrichment_result.estimated_intent,
            urgency_level=enrichment_result.urgency_level,
            pain_points=enrichment_result.pain_points,
            ai_summary=enrichment_result.ai_summary,
            raw_llm_response=raw_response,
        )
        session.add(enrichment)

        # Update execution log
        exec_log.status = "SUCCESS"
        exec_log.duration_ms = duration_ms

        # Update lead status
        lead.status = "ENRICHED"
        lead.updated_at = datetime.now(timezone.utc)

        session.flush()
        log.info("Enrichment completed", duration_ms=duration_ms, category=enrichment_result.lead_category)

        return enrichment_result

    except LLMError as e:
        duration_ms = int((time.time() - start_time) * 1000)
        log.error("Enrichment failed", error=str(e), duration_ms=duration_ms)

        # Fall back to mock (keyword-based) enrichment using actual lead data
        # This guarantees intelligent results even when the API is down
        from app.services.llm_client import _mock_enrichment
        fallback, fallback_raw = _mock_enrichment(
            name=lead.name,
            email=lead.email,
            company=lead.company,
            message=lead.message,
            source=lead.source,
            lead_id=str(lead.id),
        )

        enrichment = Enrichment(
            lead_id=lead.id,
            lead_category=fallback.lead_category,
            company_type=fallback.company_type,
            estimated_intent=fallback.estimated_intent,
            urgency_level=fallback.urgency_level,
            pain_points=fallback.pain_points,
            ai_summary=fallback.ai_summary,
            raw_llm_response=f"FALLBACK_MOCK: {fallback_raw}",
        )
        session.add(enrichment)

        # Update execution log
        exec_log.status = "FAILED"
        exec_log.duration_ms = duration_ms
        exec_log.error_message = str(e)
        exec_log.error_traceback = traceback.format_exc()

        # Flag for review but continue pipeline with mock data
        lead.flag_for_review = True
        lead.flag_reason = f"Gemini API failed, used mock enrichment: {str(e)}"
        lead.status = "ENRICHED"  # Continue with fallback
        lead.updated_at = datetime.now(timezone.utc)

        session.flush()
        log.warning("Using mock enrichment fallback, lead flagged for review")

        return fallback

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        log.error("Unexpected enrichment error", error=str(e), error_type=type(e).__name__)

        exec_log.status = "FAILED"
        exec_log.duration_ms = duration_ms
        exec_log.error_message = str(e)
        exec_log.error_traceback = traceback.format_exc()
        session.flush()

        raise
