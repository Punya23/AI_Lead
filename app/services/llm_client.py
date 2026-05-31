"""
LLM client — Google Gemini wrapper with retry logic and failure simulation.

Handles:
- Structured JSON output via response_schema
- Exponential backoff on timeouts
- Corrective prompt retries on malformed responses
- Failure simulation for demo purposes
- MOCK MODE: If GOOGLE_API_KEY is not set, uses deterministic
  keyword-based enrichment so the project works without any API key.
  This is critical for evaluators who want to run `docker compose up`
  and see the full pipeline immediately.
"""

import json
import random
import re
import time

from loguru import logger
from pydantic import ValidationError

from app.core.config import settings
from app.core.exceptions import (
    LLMMalformedResponseError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from app.schemas.enrichment import EnrichmentResult
from app.tasks.retry_policies import LLM_RETRY_POLICY


def _is_api_key_configured() -> bool:
    """Check if a real Gemini API key is configured.

    Returns False if:
    - GOOGLE_API_KEY is empty
    - GOOGLE_API_KEY is the placeholder from .env.example
    - GOOGLE_API_KEY is not set at all
    """
    key = settings.GOOGLE_API_KEY
    if not key:
        return False
    if key in ("your-gemini-api-key-here", ""):
        return False
    return True


# =========================================================================
# Mock Enrichment (no API key needed)
# =========================================================================

def _mock_enrichment(
    name: str,
    email: str,
    company: str,
    message: str,
    source: str | None = None,
    lead_id: str | None = None,
) -> tuple[EnrichmentResult, str]:
    """Deterministic keyword-based enrichment — no API key needed.

    This analyzes the message text using pattern matching and keyword
    detection to produce enrichment results that are realistic enough
    to demonstrate the full pipeline (scoring → routing → notifications).

    This is NOT a placeholder. It's a rule-based analysis engine that:
    - Detects urgency from time-related keywords
    - Extracts pain points from cost/problem language
    - Classifies intent from action keywords
    - Categorizes the company from domain and message context

    Args:
        name, email, company, message, source, lead_id: Lead data.

    Returns:
        tuple: (EnrichmentResult, raw_response_json_string)
    """
    log = logger.bind(lead_id=lead_id, mode="mock")
    log.info("Using mock enrichment (no GOOGLE_API_KEY configured)")

    msg_lower = message.lower()
    company_lower = company.lower()

    # --- Urgency Detection ---
    high_urgency_signals = [
        "asap", "urgent", "immediately", "deadline", "within days",
        "this week", "right away", "critical", "emergency", "90 days",
        "60 days", "30 days", "end of quarter", "paid pilot",
    ]
    medium_urgency_signals = [
        "soon", "this quarter", "next month", "planning", "looking to",
        "need to", "want to", "exploring options",
    ]

    urgency = "Low"
    if any(signal in msg_lower for signal in high_urgency_signals):
        urgency = "High"
    elif any(signal in msg_lower for signal in medium_urgency_signals):
        urgency = "Medium"

    # --- Intent Classification ---
    high_intent_keywords = [
        "demo", "pricing", "pilot", "trial", "deploy", "implement",
        "purchase", "buy", "contract", "proposal", "budget approved",
        "ready to start", "paid", "integrate",
    ]
    medium_intent_keywords = [
        "interested", "learn more", "case study", "capabilities",
        "explore", "evaluate", "compare", "research",
    ]

    intent = "General Inquiry"
    if any(kw in msg_lower for kw in high_intent_keywords):
        intent = "Demo Request"
    elif any(kw in msg_lower for kw in medium_intent_keywords):
        intent = "Product Inquiry"

    # --- Pain Point Extraction ---
    pain_points = []
    pain_patterns = [
        (r"(\d+[\+]?\s*(?:hours?|hrs?).*?(?:per|a)\s*(?:day|week))", "Time waste: {}"),
        (r"\$\d+[KkMm]?(?:/year|/month| annually| per year)?", "Cost issue: {}"),
        (r"(\d+[\+]?\s*(?:tickets?|records?|pipelines?|customers?|orders?).*?(?:daily|per day|per month))", "High volume: {}"),
        (r"manual(?:ly)?[\s\w]*(?:process|triage|entry|work)", "Manual process burden"),
        (r"(?:losing|wasting|spending)\s+\d+", "Resource drain identified"),
        (r"(?:scale|scaling|growth)\s+(?:from|our)", "Scaling challenge"),
    ]
    for pattern, template in pain_patterns:
        match = re.search(pattern, message, re.IGNORECASE)
        if match:
            if "{}" in template:
                pain_points.append(template.format(match.group(0).strip()))
            else:
                pain_points.append(template)

    if not pain_points and len(message) > 50:
        pain_points.append("Seeking AI/automation solution")

    # --- Company Category ---
    category = "B2B"
    if any(kw in company_lower for kw in ["saas", "software", "tech", "digital", "platform", "app"]):
        category = "B2B SaaS"
    elif any(kw in company_lower for kw in ["health", "med", "hospital", "clinic", "pharma"]):
        category = "Healthcare"
    elif any(kw in company_lower for kw in ["finance", "bank", "capital", "invest"]):
        category = "Finance"
    elif any(kw in company_lower for kw in ["university", "school", "research", "lab", "edu"]):
        category = "Education/Research"
    elif any(kw in msg_lower for kw in ["saas", "software", "platform", "api"]):
        category = "B2B SaaS"

    # --- Company Type ---
    company_type = "Enterprise"
    if any(kw in msg_lower for kw in ["startup", "series a", "series b", "seed", "early-stage"]):
        company_type = "Startup"
    elif any(kw in msg_lower for kw in ["mid-size", "growing", "200-person", "100-person"]):
        company_type = "Mid-Market"
    elif "university" in company_lower or "research" in company_lower:
        company_type = "Academic"

    # --- AI Summary ---
    summary = (
        f"{company} ({company_type}) is reaching out via {source or 'unknown channel'}. "
        f"Intent: {intent}. Urgency: {urgency}. "
        f"{'Key pain points: ' + '; '.join(pain_points[:3]) + '.' if pain_points else 'No specific pain points identified.'} "
        f"[Mock enrichment — set GOOGLE_API_KEY for AI-powered analysis]"
    )

    result = EnrichmentResult(
        lead_category=category,
        company_type=company_type,
        estimated_intent=intent,
        urgency_level=urgency,
        pain_points=pain_points[:5],
        ai_summary=summary,
    )

    raw_json = json.dumps(result.model_dump(), indent=2)
    log.info(
        "Mock enrichment complete",
        category=category,
        intent=intent,
        urgency=urgency,
        pain_points_count=len(pain_points),
    )

    return result, raw_json


# =========================================================================
# Real LLM Client (Gemini)
# =========================================================================

def _get_client():
    """Create a Gemini client instance.

    Returns:
        genai.Client: Configured Gemini client.
    """
    from google import genai
    return genai.Client(api_key=settings.GOOGLE_API_KEY)


def _maybe_simulate_failure(lead_id: str | None = None) -> None:
    """Simulate LLM failures when SIMULATE_FAILURES is enabled.

    Args:
        lead_id: Lead ID for logging context.

    Raises:
        LLMTimeoutError: Simulated timeout.
        LLMMalformedResponseError: Simulated malformed response.
        LLMRateLimitError: Simulated rate limit.
    """
    simulate = settings.SIMULATE_FAILURES
    try:
        import redis
        r = redis.from_url(settings.REDIS_URL)
        val = r.get("SIMULATE_FAILURES")
        if val is not None:
            simulate = (val.decode('utf-8').lower() == "true")
        r.close()
    except Exception as e:
        logger.warning(f"Could not connect to Redis to check failure simulation flag: {e}")

    if not simulate:
        return

    roll = random.random()

    if roll < settings.FAILURE_RATE_LLM_TIMEOUT:
        logger.warning("SIMULATED: LLM timeout", lead_id=lead_id)
        raise LLMTimeoutError(lead_id=lead_id, timeout_seconds=settings.LLM_TIMEOUT_SECONDS)

    if roll < settings.FAILURE_RATE_LLM_TIMEOUT + settings.FAILURE_RATE_MALFORMED_RESPONSE:
        logger.warning("SIMULATED: Malformed LLM response", lead_id=lead_id)
        raise LLMMalformedResponseError(lead_id=lead_id, raw_response='{"invalid": true}')

    if roll < (settings.FAILURE_RATE_LLM_TIMEOUT + settings.FAILURE_RATE_MALFORMED_RESPONSE
               + settings.FAILURE_RATE_RATE_LIMIT):
        logger.warning("SIMULATED: LLM rate limit", lead_id=lead_id)
        raise LLMRateLimitError(lead_id=lead_id, retry_after=5)


def _build_enrichment_prompt(name: str, email: str, company: str, message: str, source: str | None) -> str:
    """Build the enrichment prompt with few-shot example.

    Args:
        name: Lead's name.
        email: Lead's email.
        company: Lead's company.
        message: Lead's message.
        source: Lead's source channel.

    Returns:
        str: Complete prompt for the Gemini API.
    """
    return f"""You are a B2B lead analysis expert. Analyze the following inbound lead and generate a structured enrichment profile.

EXAMPLE INPUT:
Name: Sarah Chen
Email: sarah@techflow.io
Company: TechFlow Solutions
Message: We're a mid-size SaaS company processing 1000+ customer tickets daily. Looking for AI automation to reduce response times and agent workload. Need something production-ready within 2 months.
Source: website_form

EXAMPLE OUTPUT:
{{
  "lead_category": "B2B SaaS",
  "company_type": "Customer Support Platform",
  "estimated_intent": "Demo Request",
  "urgency_level": "High",
  "pain_points": ["High ticket volume overwhelming agents", "Slow response times", "Need for production-ready solution within tight timeline"],
  "ai_summary": "TechFlow Solutions is a mid-size SaaS company handling 1000+ daily customer tickets. They are actively seeking AI automation to reduce response times and agent workload, with a 2-month implementation timeline indicating high urgency."
}}

NOW ANALYZE THIS LEAD:
Name: {name}
Email: {email}
Company: {company}
Message: {message}
Source: {source or 'unknown'}

Generate the enrichment profile as a JSON object. Be specific and grounded in the actual lead data — do not hallucinate details not present in the input."""


def call_enrichment_llm(
    name: str,
    email: str,
    company: str,
    message: str,
    source: str | None = None,
    lead_id: str | None = None,
) -> tuple[EnrichmentResult, str]:
    """Call Gemini to enrich a lead with structured data.

    If GOOGLE_API_KEY is not configured, automatically falls back to
    mock enrichment (keyword-based analysis). This ensures the project
    works out of the box for evaluators.

    Implements retry logic with exponential backoff for timeouts
    and corrective prompts for malformed responses.

    Args:
        name: Lead's name.
        email: Lead's email.
        company: Lead's company.
        message: Lead's message.
        source: Lead's source channel.
        lead_id: Lead UUID for logging context.

    Returns:
        tuple: (EnrichmentResult, raw_response_text)

    Raises:
        LLMTimeoutError: After all retries exhausted on timeout.
        LLMMalformedResponseError: After all retries exhausted on parse failure.
        LLMRateLimitError: On rate limiting.
    """
    # --- MOCK MODE: No API key → use deterministic enrichment ---
    if not _is_api_key_configured():
        return _mock_enrichment(name, email, company, message, source, lead_id)

    # --- REAL MODE: Call Gemini API ---
    from google.genai.types import GenerateContentConfig

    client = _get_client()
    prompt = _build_enrichment_prompt(name, email, company, message, source)

    max_attempts = LLM_RETRY_POLICY["max_attempts"]
    delay = LLM_RETRY_POLICY["initial_delay_seconds"]
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            logger.info(
                "LLM enrichment call",
                lead_id=lead_id,
                attempt=attempt,
                model=settings.LLM_MODEL,
            )

            # Simulate failures if enabled
            _maybe_simulate_failure(lead_id=lead_id)

            # Call Gemini with structured output
            response = client.models.generate_content(
                model=settings.LLM_MODEL,
                contents=prompt,
                config=GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=EnrichmentResult,
                    temperature=settings.LLM_TEMPERATURE,
                ),
            )

            raw_text = response.text
            logger.debug("LLM raw response", lead_id=lead_id, response_length=len(raw_text))

            # Clean markdown code blocks if present
            clean_text = raw_text.strip()
            if clean_text.startswith("```"):
                lines = clean_text.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                clean_text = "\n".join(lines).strip()

            # Parse and validate against Pydantic schema
            parsed = json.loads(clean_text)
            result = EnrichmentResult(**parsed)

            logger.info(
                "LLM enrichment success",
                lead_id=lead_id,
                attempt=attempt,
                category=result.lead_category,
                intent=result.estimated_intent,
            )

            return result, raw_text

        except (json.JSONDecodeError, ValidationError) as e:
            last_error = LLMMalformedResponseError(lead_id=lead_id, raw_response=str(e))
            logger.warning(
                "LLM response validation failed",
                lead_id=lead_id,
                attempt=attempt,
                error=str(e),
                raw_text=raw_text,
            )

            # Add corrective suffix for next attempt
            if attempt < max_attempts:
                prompt += (
                    "\n\nIMPORTANT: Your previous response was not valid JSON. "
                    "Respond ONLY with the JSON object, no additional text or markdown."
                )
                time.sleep(delay)
                delay *= LLM_RETRY_POLICY["backoff_multiplier"]

        except LLMTimeoutError:
            last_error = LLMTimeoutError(lead_id=lead_id, timeout_seconds=settings.LLM_TIMEOUT_SECONDS)
            logger.warning(
                "LLM timeout",
                lead_id=lead_id,
                attempt=attempt,
                timeout=settings.LLM_TIMEOUT_SECONDS,
            )
            if attempt < max_attempts:
                time.sleep(delay)
                delay *= LLM_RETRY_POLICY["backoff_multiplier"]

        except LLMRateLimitError as e:
            last_error = e
            logger.warning(
                "LLM rate limited",
                lead_id=lead_id,
                attempt=attempt,
                retry_after=e.retry_after,
            )
            if attempt < max_attempts:
                time.sleep(max(delay, e.retry_after or 5))
                delay *= LLM_RETRY_POLICY["backoff_multiplier"]

        except Exception as e:
            last_error = LLMMalformedResponseError(lead_id=lead_id, raw_response=str(e))
            logger.error(
                "LLM unexpected error",
                lead_id=lead_id,
                attempt=attempt,
                error=str(e),
                error_type=type(e).__name__,
            )
            if attempt < max_attempts:
                time.sleep(delay)
                delay *= LLM_RETRY_POLICY["backoff_multiplier"]

    # All retries exhausted
    logger.error(
        "LLM enrichment failed after all retries",
        lead_id=lead_id,
        max_attempts=max_attempts,
    )
    raise last_error


def get_fallback_enrichment() -> EnrichmentResult:
    """Return fallback enrichment data when LLM fails after all retries.

    Used when flag_for_review is set. Provides safe defaults that
    won't break downstream scoring/routing.

    Returns:
        EnrichmentResult: Conservative fallback enrichment.
    """
    return EnrichmentResult(
        lead_category="Unknown",
        company_type="Unknown",
        estimated_intent="Unknown",
        urgency_level="Low",
        pain_points=[],
        ai_summary="Enrichment failed after maximum retries. Lead flagged for manual review.",
    )
