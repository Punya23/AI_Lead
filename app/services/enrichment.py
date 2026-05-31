"""
Enrichment service — Multi-Agent execution.

Provides 3 distinct agents:
1. Intent & Urgency Agent
2. Research Agent (Company Context)
3. Categorization Agent

Includes lightweight JSONB checkpointing to ensure idempotency
across Celery task retries.
"""

import time
import traceback
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from app.core.exceptions import LLMError
from app.models.enrichment import Enrichment
from app.models.execution_log import ExecutionLog
from app.models.lead import Lead
from app.schemas.enrichment import (
    IntentUrgencyResult,
    CompanyContextResult,
    CategorizationResult,
    EnrichmentResult,
)
from app.services.llm_client import (
    call_intent_agent,
    call_research_agent,
    call_categorization_agent,
    get_fallback_enrichment,
)


def _log_execution_start(session: Session, lead_id: str, stage: str) -> ExecutionLog:
    exec_log = ExecutionLog(
        lead_id=lead_id,
        stage=stage,
        status="STARTED",
        attempt_number=1,
    )
    session.add(exec_log)
    session.flush()
    return exec_log


def _update_execution_log(session: Session, exec_log: ExecutionLog, status: str, duration_ms: int, error: str = None) -> None:
    exec_log.status = status
    exec_log.duration_ms = duration_ms
    if error:
        exec_log.error_message = error
        exec_log.error_traceback = traceback.format_exc()
    session.flush()


def run_enrichment_agent(session: Session, lead: Lead, message: str) -> IntentUrgencyResult:
    """Agent 1: Extracts intent, urgency, and pain points."""
    lead_id = str(lead.id)
    log = logger.bind(lead_id=lead_id, agent="intent")
    start_time = time.time()

    # 1. Check checkpoint (idempotent resume)
    checkpoint = lead.pipeline_checkpoint or {}
    if "enrichment" in checkpoint:
        log.info("Skipping intent agent (found in checkpoint)")
        return IntentUrgencyResult(**checkpoint["enrichment"])

    exec_log = _log_execution_start(session, lead_id, "agent_intent")

    try:
        # 2. Call LLM
        result, raw = call_intent_agent(message, lead_id=lead_id)
        
        # 3. Update checkpoint
        checkpoint["enrichment"] = result.model_dump()
        lead.pipeline_checkpoint = checkpoint
        flag_modified(lead, "pipeline_checkpoint")
        
        duration_ms = int((time.time() - start_time) * 1000)
        _update_execution_log(session, exec_log, "SUCCESS", duration_ms)
        session.flush()
        
        return result

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        _update_execution_log(session, exec_log, "FAILED", duration_ms, str(e))
        raise


def run_research_agent(session: Session, lead: Lead, domain: str, message: str) -> CompanyContextResult:
    """Agent 2: Infers company type and generates AI summary."""
    lead_id = str(lead.id)
    log = logger.bind(lead_id=lead_id, agent="research")
    start_time = time.time()

    # 1. Check checkpoint (idempotent resume)
    checkpoint = lead.pipeline_checkpoint or {}
    if "research" in checkpoint:
        log.info("Skipping research agent (found in checkpoint)")
        return CompanyContextResult(**checkpoint["research"])

    exec_log = _log_execution_start(session, lead_id, "agent_research")

    try:
        # 2. Call LLM
        result, raw = call_research_agent(domain, message, lead_id=lead_id)
        
        # 3. Update checkpoint
        checkpoint["research"] = result.model_dump()
        lead.pipeline_checkpoint = checkpoint
        flag_modified(lead, "pipeline_checkpoint")
        
        duration_ms = int((time.time() - start_time) * 1000)
        _update_execution_log(session, exec_log, "SUCCESS", duration_ms)
        session.flush()
        
        return result

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        _update_execution_log(session, exec_log, "FAILED", duration_ms, str(e))
        raise


def run_categorization_agent(
    session: Session, 
    lead: Lead, 
    intent_result: IntentUrgencyResult, 
    research_result: CompanyContextResult, 
    message: str
) -> EnrichmentResult:
    """Agent 3: Classifies category and finalizes enrichment."""
    lead_id = str(lead.id)
    log = logger.bind(lead_id=lead_id, agent="categorization")
    start_time = time.time()

    # 1. Check checkpoint (idempotent resume)
    checkpoint = lead.pipeline_checkpoint or {}
    if "categorization" in checkpoint:
        log.info("Skipping categorization agent (found in checkpoint)")
        combined = checkpoint["categorization"]
        return EnrichmentResult(**combined)

    exec_log = _log_execution_start(session, lead_id, "agent_categorization")

    try:
        # 2. Call LLM
        cat_result, raw = call_categorization_agent(
            intent=intent_result.estimated_intent,
            company=research_result.company_type,
            message=message,
            lead_id=lead_id
        )
        
        # 3. Combine into final result
        final_result = EnrichmentResult(
            lead_category=cat_result.lead_category,
            company_type=research_result.company_type,
            estimated_intent=intent_result.estimated_intent,
            urgency_level=intent_result.urgency_level,
            pain_points=intent_result.pain_points,
            ai_summary=research_result.ai_summary,
        )
        
        # 4. Save to Database
        enrichment = Enrichment(
            lead_id=lead.id,
            lead_category=final_result.lead_category,
            company_type=final_result.company_type,
            estimated_intent=final_result.estimated_intent,
            urgency_level=final_result.urgency_level,
            pain_points=final_result.pain_points,
            ai_summary=final_result.ai_summary,
            raw_llm_response=raw,
        )
        session.add(enrichment)
        
        # Update lead status
        lead.status = "ENRICHED"
        lead.updated_at = datetime.now(timezone.utc)

        # 5. Update checkpoint
        checkpoint["categorization"] = final_result.model_dump()
        lead.pipeline_checkpoint = checkpoint
        flag_modified(lead, "pipeline_checkpoint")
        
        duration_ms = int((time.time() - start_time) * 1000)
        _update_execution_log(session, exec_log, "SUCCESS", duration_ms)
        session.flush()
        
        return final_result

    except Exception as e:
        duration_ms = int((time.time() - start_time) * 1000)
        _update_execution_log(session, exec_log, "FAILED", duration_ms, str(e))
        raise


def handle_fallback_enrichment(session: Session, lead: Lead, error_message: str) -> EnrichmentResult:
    """Triggered by the error_node when all retries are exhausted."""
    fallback = get_fallback_enrichment()
    
    enrichment = Enrichment(
        lead_id=lead.id,
        lead_category=fallback.lead_category,
        company_type=fallback.company_type,
        estimated_intent=fallback.estimated_intent,
        urgency_level=fallback.urgency_level,
        pain_points=fallback.pain_points,
        ai_summary=fallback.ai_summary,
        raw_llm_response=f"FALLBACK_MOCK: {error_message}",
    )
    session.add(enrichment)
    
    lead.flag_for_review = True
    lead.flag_reason = f"Agent failure: {error_message}"
    lead.status = "ENRICHED"  # Continue pipeline with fallback
    lead.updated_at = datetime.now(timezone.utc)
    
    session.flush()
    return fallback
