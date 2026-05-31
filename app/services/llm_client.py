"""
LLM client — Google Gemini wrapper with retry logic and failure simulation.

Handles:
- Structured JSON output via response_schema
- Exponential backoff on timeouts
- Corrective prompt retries on malformed responses
- Failure simulation for demo purposes
- MOCK MODE: If GOOGLE_API_KEY is not set, uses deterministic
  keyword-based enrichment so the project works without any API key.
"""

import json
import random
import re
import time

from loguru import logger
from pydantic import BaseModel, ValidationError

from app.core.config import settings
from app.core.exceptions import (
    LLMMalformedResponseError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.schemas.enrichment import (
    IntentUrgencyResult,
    CompanyContextResult,
    CategorizationResult,
    EnrichmentResult,
)
from app.tasks.retry_policies import LLM_RETRY_POLICY


def _is_api_key_configured() -> bool:
    key = settings.GOOGLE_API_KEY
    if not key:
        return False
    if key in ("your-gemini-api-key-here", ""):
        return False
    return True


# =========================================================================
# Mock Enrichment Agents (no API key needed)
# =========================================================================

def _mock_intent_agent(message: str, lead_id: str | None = None) -> tuple[IntentUrgencyResult, str]:
    log = logger.bind(lead_id=lead_id, mode="mock", agent="intent")
    log.info("Using mock intent agent")

    msg_lower = message.lower()

    # Urgency
    high_urgency = ["asap", "urgent", "immediately", "deadline", "within days"]
    med_urgency = ["soon", "this quarter", "next month", "planning"]
    urgency = "Low"
    if any(s in msg_lower for s in high_urgency):
        urgency = "High"
    elif any(s in msg_lower for s in med_urgency):
        urgency = "Medium"

    # Intent
    high_intent = ["demo", "pricing", "pilot", "purchase", "budget approved"]
    med_intent = ["interested", "learn more", "explore", "evaluate"]
    intent = "General Inquiry"
    if any(kw in msg_lower for kw in high_intent):
        intent = "Demo Request"
    elif any(kw in msg_lower for kw in med_intent):
        intent = "Product Inquiry"

    # Pain points
    pain_points = []
    if len(message) > 50:
        pain_points.append("Seeking AI/automation solution")

    result = IntentUrgencyResult(
        estimated_intent=intent,
        urgency_level=urgency,
        pain_points=pain_points[:5],
    )
    return result, json.dumps(result.model_dump(), indent=2)


def _mock_research_agent(domain: str, message: str, lead_id: str | None = None) -> tuple[CompanyContextResult, str]:
    log = logger.bind(lead_id=lead_id, mode="mock", agent="research")
    log.info("Using mock research agent")

    msg_lower = message.lower()
    
    company_type = "Enterprise"
    if any(kw in msg_lower for kw in ["startup", "seed", "early-stage"]):
        company_type = "Startup"
    elif any(kw in msg_lower for kw in ["mid-size", "growing"]):
        company_type = "Mid-Market"

    summary = f"Company at {domain} ({company_type}) is inquiring. [Mock Research]."
    
    result = CompanyContextResult(
        company_type=company_type,
        ai_summary=summary,
    )
    return result, json.dumps(result.model_dump(), indent=2)


def _mock_categorization_agent(intent: str, company: str, message: str, lead_id: str | None = None) -> tuple[CategorizationResult, str]:
    log = logger.bind(lead_id=lead_id, mode="mock", agent="categorization")
    log.info("Using mock categorization agent")

    msg_lower = message.lower()
    company_lower = company.lower()
    
    category = "B2B"
    if any(kw in company_lower or kw in msg_lower for kw in ["saas", "software", "tech"]):
        category = "B2B SaaS"
    elif any(kw in company_lower for kw in ["health", "med", "clinic"]):
        category = "Healthcare"

    result = CategorizationResult(
        lead_category=category,
    )
    return result, json.dumps(result.model_dump(), indent=2)


# =========================================================================
# Real LLM Client (Gemini)
# =========================================================================

def _get_client():
    from google import genai
    return genai.Client(api_key=settings.GOOGLE_API_KEY)


def _maybe_simulate_failure(lead_id: str | None = None) -> None:
    simulate = settings.SIMULATE_FAILURES
    try:
        import redis
        r = redis.from_url(settings.REDIS_URL)
        val = r.get("SIMULATE_FAILURES")
        if val is not None:
            simulate = (val.decode('utf-8').lower() == "true")
        r.close()
    except Exception:
        pass

    if not simulate:
        return

    roll = random.random()
    if roll < settings.FAILURE_RATE_LLM_TIMEOUT:
        raise LLMTimeoutError(lead_id=lead_id, timeout_seconds=settings.LLM_TIMEOUT_SECONDS)
    if roll < settings.FAILURE_RATE_LLM_TIMEOUT + settings.FAILURE_RATE_MALFORMED_RESPONSE:
        raise LLMMalformedResponseError(lead_id=lead_id, raw_response='{"invalid": true}')
    if roll < (settings.FAILURE_RATE_LLM_TIMEOUT + settings.FAILURE_RATE_MALFORMED_RESPONSE
               + settings.FAILURE_RATE_RATE_LIMIT):
        raise LLMRateLimitError(lead_id=lead_id, retry_after=5)


def _execute_llm_call(prompt: str, schema_class: type[BaseModel], lead_id: str | None, agent_name: str) -> tuple[BaseModel, str]:
    """Execute LLM call with retry logic and schema validation."""
    from google.genai.types import GenerateContentConfig

    client = _get_client()
    max_attempts = LLM_RETRY_POLICY["max_attempts"]
    delay = LLM_RETRY_POLICY["initial_delay_seconds"]
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info("LLM agent call", lead_id=lead_id, agent=agent_name, attempt=attempt)
            _maybe_simulate_failure(lead_id=lead_id)

            response = client.models.generate_content(
                model=settings.LLM_MODEL,
                contents=prompt,
                config=GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=schema_class,
                    temperature=settings.LLM_TEMPERATURE,
                ),
            )
            raw_text = response.text
            
            clean_text = raw_text.strip()
            if clean_text.startswith("```"):
                lines = clean_text.split("\n")
                if lines[0].startswith("```"): lines = lines[1:]
                if lines and lines[-1].startswith("```"): lines = lines[:-1]
                clean_text = "\n".join(lines).strip()

            parsed = json.loads(clean_text)
            result = schema_class(**parsed)
            logger.info("LLM agent success", lead_id=lead_id, agent=agent_name)
            return result, raw_text

        except (json.JSONDecodeError, ValidationError) as e:
            last_error = LLMMalformedResponseError(lead_id=lead_id, raw_response=str(e))
            if attempt < max_attempts:
                prompt += "\n\nIMPORTANT: Respond ONLY with the JSON object."
                time.sleep(delay)
                delay *= LLM_RETRY_POLICY["backoff_multiplier"]

        except LLMTimeoutError:
            last_error = LLMTimeoutError(lead_id=lead_id, timeout_seconds=settings.LLM_TIMEOUT_SECONDS)
            if attempt < max_attempts:
                time.sleep(delay)
                delay *= LLM_RETRY_POLICY["backoff_multiplier"]

        except LLMRateLimitError as e:
            last_error = e
            if attempt < max_attempts:
                time.sleep(max(delay, e.retry_after or 5))
                delay *= LLM_RETRY_POLICY["backoff_multiplier"]

        except Exception as e:
            # Handle genai ClientErrors (we import locally to avoid hard dependency if using mock)
            try:
                from google.genai.errors import ClientError
                if isinstance(e, ClientError):
                    if e.code == 429:
                        last_error = LLMRateLimitError(lead_id=lead_id, retry_after=5)
                        if attempt < max_attempts:
                            time.sleep(max(delay, 5))
                            delay *= LLM_RETRY_POLICY["backoff_multiplier"]
                            continue
            except ImportError:
                pass
                
            last_error = LLMMalformedResponseError(lead_id=lead_id, raw_response=str(e))
            if attempt < max_attempts:
                time.sleep(delay)
                delay *= LLM_RETRY_POLICY["backoff_multiplier"]

    logger.error("LLM agent failed after all retries", lead_id=lead_id, agent=agent_name)
    raise last_error


# --- Agent Specific Callers ---

def call_intent_agent(message: str, lead_id: str | None = None) -> tuple[IntentUrgencyResult, str]:
    if not _is_api_key_configured():
        return _mock_intent_agent(message, lead_id)
        
    prompt = f"Analyze the following lead message to extract intent, urgency, and pain points.\nMessage: {message}"
    result, raw = _execute_llm_call(prompt, IntentUrgencyResult, lead_id, "intent")
    return result, raw


def call_research_agent(domain: str, message: str, lead_id: str | None = None) -> tuple[CompanyContextResult, str]:
    if not _is_api_key_configured():
        return _mock_research_agent(domain, message, lead_id)
        
    prompt = f"Analyze this lead from domain {domain} and their message to infer company type and write a brief AI summary.\nMessage: {message}"
    result, raw = _execute_llm_call(prompt, CompanyContextResult, lead_id, "research")
    return result, raw


def call_categorization_agent(intent: str, company: str, message: str, lead_id: str | None = None) -> tuple[CategorizationResult, str]:
    if not _is_api_key_configured():
        return _mock_categorization_agent(intent, company, message, lead_id)
        
    prompt = f"Based on intent '{intent}' and company '{company}', classify the lead category.\nMessage: {message}"
    result, raw = _execute_llm_call(prompt, CategorizationResult, lead_id, "categorization")
    return result, raw


def get_fallback_enrichment() -> EnrichmentResult:
    return EnrichmentResult(
        lead_category="Unknown",
        company_type="Unknown",
        estimated_intent="Unknown",
        urgency_level="Low",
        pain_points=[],
        ai_summary="Enrichment failed. Lead flagged.",
    )
