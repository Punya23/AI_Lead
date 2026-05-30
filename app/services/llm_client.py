"""
LLM client — Google Gemini wrapper with retry logic and failure simulation.

Handles:
- Structured JSON output via response_schema
- Exponential backoff on timeouts
- Corrective prompt retries on malformed responses
- Failure simulation for demo purposes
"""

import json
import random
import time

from google import genai
from google.genai.types import GenerateContentConfig
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


def _get_client() -> genai.Client:
    """Create a Gemini client instance.

    Returns:
        genai.Client: Configured Gemini client.
    """
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
    if not settings.SIMULATE_FAILURES:
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
                    temperature=settings.LLM_TEMPERATURE,
                ),
            )

            raw_text = response.text
            logger.debug("LLM raw response", lead_id=lead_id, response_length=len(raw_text))

            # Parse and validate against Pydantic schema
            parsed = json.loads(raw_text)
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
