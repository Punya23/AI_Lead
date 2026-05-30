"""
Test fixtures and configuration.

Provides mocked LLM client, test database session, and sample lead data.
"""

import pytest

from app.schemas.enrichment import EnrichmentResult


# =============================================================================
# Sample Lead Data
# =============================================================================

VALID_LEAD_DATA = {
    "name": "Jane Smith",
    "email": "jane@acmecorp.com",
    "company": "Acme Corp",
    "message": "We need AI automation for our customer support pipeline. Currently handling 500+ tickets/day manually and looking to reduce response times by 50%.",
    "source": "website_form",
}

SPAM_LEAD_DATA = {
    "name": "Test",
    "email": "spam@mailinator.com",
    "company": "Free Money Inc",
    "message": "Buy now! Limited offer! Click here for free money!",
    "source": "api",
}

INCOMPLETE_LEAD_DATA = {
    "name": "",
    "email": "test@example.com",
    "company": "TestCo",
    "message": "Hello",
    "source": "api",
}

HIGH_INTENT_ENRICHMENT = EnrichmentResult(
    lead_category="B2B SaaS",
    company_type="Customer Support Platform",
    estimated_intent="Demo Request",
    urgency_level="High",
    pain_points=["High ticket volume", "Slow response times", "Agent burnout"],
    ai_summary="Acme Corp handles 500+ daily tickets and needs AI automation urgently.",
)

LOW_INTENT_ENRICHMENT = EnrichmentResult(
    lead_category="Individual",
    company_type="Unknown",
    estimated_intent="Unknown",
    urgency_level="Low",
    pain_points=[],
    ai_summary="Vague inquiry with no specific needs identified.",
)

MEDIUM_INTENT_ENRICHMENT = EnrichmentResult(
    lead_category="SMB",
    company_type="E-commerce",
    estimated_intent="Technical Inquiry",
    urgency_level="Medium",
    pain_points=["Manual data entry", "Inventory tracking"],
    ai_summary="Small e-commerce company exploring automation options.",
)
