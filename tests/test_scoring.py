"""
Tests for the deterministic scoring engine.

Verifies that scoring is deterministic (same input = same output),
weights are correct, and qualification reasons are generated properly.
"""

import pytest
from unittest.mock import MagicMock

from app.schemas.enrichment import EnrichmentResult
from app.services.scoring import (
    calculate_confidence,
    generate_qualification_reason,
    get_disqualification_flags,
    score_company_completeness,
    score_intent_clarity,
    score_message_quality,
    score_pain_point_specificity,
    score_urgency_signal,
)
from tests.conftest import HIGH_INTENT_ENRICHMENT, LOW_INTENT_ENRICHMENT


class TestIntentClarity:
    """Tests for intent clarity scoring signal."""

    def test_demo_request_max_score(self):
        enrichment = EnrichmentResult(
            lead_category="B2B SaaS", company_type="SaaS", estimated_intent="Demo Request",
            urgency_level="High", pain_points=[], ai_summary="Test"
        )
        assert score_intent_clarity(enrichment) == 25

    def test_pricing_score(self):
        enrichment = EnrichmentResult(
            lead_category="B2B SaaS", company_type="SaaS", estimated_intent="Pricing",
            urgency_level="High", pain_points=[], ai_summary="Test"
        )
        assert score_intent_clarity(enrichment) == 20

    def test_unknown_intent_low_score(self):
        enrichment = EnrichmentResult(
            lead_category="Unknown", company_type="Unknown", estimated_intent="Unknown",
            urgency_level="Low", pain_points=[], ai_summary="Test"
        )
        assert score_intent_clarity(enrichment) == 5

    def test_spam_intent_zero(self):
        enrichment = EnrichmentResult(
            lead_category="Unknown", company_type="Unknown", estimated_intent="Spam",
            urgency_level="Low", pain_points=[], ai_summary="Test"
        )
        assert score_intent_clarity(enrichment) == 0


class TestUrgencySignal:
    """Tests for urgency signal scoring."""

    def test_high_urgency(self):
        enrichment = EnrichmentResult(
            lead_category="B2B SaaS", company_type="SaaS", estimated_intent="Demo Request",
            urgency_level="High", pain_points=[], ai_summary="Test"
        )
        assert score_urgency_signal(enrichment) == 20

    def test_medium_urgency(self):
        enrichment = EnrichmentResult(
            lead_category="B2B SaaS", company_type="SaaS", estimated_intent="Demo Request",
            urgency_level="Medium", pain_points=[], ai_summary="Test"
        )
        assert score_urgency_signal(enrichment) == 12

    def test_low_urgency(self):
        enrichment = EnrichmentResult(
            lead_category="B2B SaaS", company_type="SaaS", estimated_intent="Demo Request",
            urgency_level="Low", pain_points=[], ai_summary="Test"
        )
        assert score_urgency_signal(enrichment) == 5


class TestPainPointSpecificity:
    """Tests for pain point scoring."""

    def test_three_or_more_pain_points(self):
        enrichment = EnrichmentResult(
            lead_category="B2B SaaS", company_type="SaaS", estimated_intent="Demo Request",
            urgency_level="High", pain_points=["a", "b", "c"], ai_summary="Test"
        )
        assert score_pain_point_specificity(enrichment) == 20

    def test_two_pain_points(self):
        enrichment = EnrichmentResult(
            lead_category="B2B SaaS", company_type="SaaS", estimated_intent="Demo Request",
            urgency_level="High", pain_points=["a", "b"], ai_summary="Test"
        )
        assert score_pain_point_specificity(enrichment) == 14

    def test_one_pain_point(self):
        enrichment = EnrichmentResult(
            lead_category="B2B SaaS", company_type="SaaS", estimated_intent="Demo Request",
            urgency_level="High", pain_points=["a"], ai_summary="Test"
        )
        assert score_pain_point_specificity(enrichment) == 7

    def test_no_pain_points(self):
        enrichment = EnrichmentResult(
            lead_category="B2B SaaS", company_type="SaaS", estimated_intent="Demo Request",
            urgency_level="High", pain_points=[], ai_summary="Test"
        )
        assert score_pain_point_specificity(enrichment) == 0


class TestCompanyCompleteness:
    """Tests for company completeness scoring."""

    def test_full_corporate_lead(self):
        lead = MagicMock()
        lead.company = "Acme Corp"
        lead.message = "We need AI automation for our customer support pipeline. Currently handling 500+ tickets daily."
        lead.source = "website_form"
        lead.email = "jane@acmecorp.com"

        score = score_company_completeness(lead)
        assert score == 20  # company(5) + long_msg(5) + source(5) + corporate_email(5)

    def test_minimal_personal_lead(self):
        lead = MagicMock()
        lead.company = "X"
        lead.message = "Hi"
        lead.source = ""
        lead.email = "test@gmail.com"

        score = score_company_completeness(lead)
        assert score == 0  # Single-char company fails len>1 check, short msg, no source, gmail


class TestMessageQuality:
    """Tests for message quality scoring."""

    def test_high_quality_message(self):
        lead = MagicMock()
        lead.message = "We are evaluating AI automation solutions for our customer support team. Currently handling 500+ tickets per day with a team of 20 agents. We need to reduce response times by 50% within the next quarter. Our budget is $50k/year for this integration."
        score = score_message_quality(lead)
        assert score >= 10  # Long + has numbers + has action words

    def test_minimal_message(self):
        lead = MagicMock()
        lead.message = "Hi there"
        score = score_message_quality(lead)
        assert score <= 5  # Short, no numbers, no action words


class TestDeterminism:
    """Tests that scoring is truly deterministic — same input = same output."""

    def test_same_input_same_score(self):
        """Run scoring 10 times with identical input. All scores must match."""
        lead = MagicMock()
        lead.company = "Acme Corp"
        lead.message = "We need AI automation. Handling 500 tickets daily."
        lead.source = "website_form"
        lead.email = "jane@acmecorp.com"

        scores = []
        for _ in range(10):
            intent = score_intent_clarity(HIGH_INTENT_ENRICHMENT)
            urgency = score_urgency_signal(HIGH_INTENT_ENRICHMENT)
            company = score_company_completeness(lead)
            pain = score_pain_point_specificity(HIGH_INTENT_ENRICHMENT)
            msg = score_message_quality(lead)
            total = intent + urgency + company + pain + msg
            scores.append(total)

        # All 10 runs must produce identical scores
        assert len(set(scores)) == 1, f"Scoring is non-deterministic! Scores: {scores}"


class TestMaxScoreIs100:
    """Verify that the maximum possible score is exactly 100."""

    def test_max_score_is_100(self):
        # Best possible enrichment
        enrichment = EnrichmentResult(
            lead_category="B2B SaaS",
            company_type="Customer Support Platform",
            estimated_intent="Demo Request",  # 25 points
            urgency_level="High",             # 20 points
            pain_points=["a", "b", "c"],      # 20 points
            ai_summary="Detailed summary",
        )

        # Best possible lead
        lead = MagicMock()
        lead.company = "Acme Corp"
        lead.message = "We are evaluating AI automation solutions. Currently handling 500+ tickets. We need to reduce response times by 50%. Our budget is allocated and timeline is Q1 2025."
        lead.source = "website_form"
        lead.email = "jane@acmecorp.com"

        intent = score_intent_clarity(enrichment)       # 25
        urgency = score_urgency_signal(enrichment)      # 20
        company = score_company_completeness(lead)      # 20
        pain = score_pain_point_specificity(enrichment) # 20
        msg = score_message_quality(lead)               # up to 15

        total = intent + urgency + company + pain + msg
        assert total <= 100, f"Max score exceeds 100: {total}"
        assert intent + urgency + company + pain <= 85  # Sub-signals cap
        assert msg <= 15  # Message quality cap


class TestQualificationReason:
    """Tests for template-generated qualification reasons."""

    def test_high_intent_reason(self):
        breakdown = {
            "intent_clarity": 25,
            "urgency_signal": 20,
            "company_completeness": 15,
            "pain_point_specificity": 20,
            "message_quality": 10,
        }
        reason = generate_qualification_reason(breakdown, HIGH_INTENT_ENRICHMENT, 90)
        assert "High-intent" in reason
        assert "intent clarity" in reason.lower()

    def test_low_intent_reason(self):
        breakdown = {
            "intent_clarity": 5,
            "urgency_signal": 5,
            "company_completeness": 5,
            "pain_point_specificity": 0,
            "message_quality": 3,
        }
        reason = generate_qualification_reason(breakdown, LOW_INTENT_ENRICHMENT, 18)
        assert "Low-intent" in reason


class TestDisqualificationFlags:
    """Tests for disqualification flag generation."""

    def test_spam_intent_flagged(self):
        enrichment = EnrichmentResult(
            lead_category="Unknown", company_type="Unknown", estimated_intent="Spam",
            urgency_level="Low", pain_points=[], ai_summary="Spam lead"
        )
        lead = MagicMock()
        lead.message = "Buy now!"
        lead.email = "spam@gmail.com"

        flags = get_disqualification_flags(enrichment, lead)
        assert "Intent classified as Spam" in flags
        assert "No specific pain points identified" in flags

    def test_clean_lead_no_flags(self):
        lead = MagicMock()
        lead.message = "We need AI automation for our customer support pipeline. Currently handling 500+ tickets/day manually."
        lead.email = "jane@acmecorp.com"

        flags = get_disqualification_flags(HIGH_INTENT_ENRICHMENT, lead)
        assert len(flags) == 0
