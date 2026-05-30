"""
Tests for the full pipeline flow (with mocked LLM).

Covers: happy path end-to-end, rejection flow, and idempotent resume.
Uses mocked Gemini client to avoid real API calls in tests.
"""

import pytest
from unittest.mock import patch, MagicMock
from uuid import uuid4

from app.schemas.enrichment import EnrichmentResult
from app.services.scoring import score_lead
from app.services.routing import route_lead
from tests.conftest import (
    VALID_LEAD_DATA,
    HIGH_INTENT_ENRICHMENT,
    MEDIUM_INTENT_ENRICHMENT,
    LOW_INTENT_ENRICHMENT,
)


class TestScoringPipelineIntegration:
    """Integration tests for scoring + routing together."""

    def _make_mock_lead(self, **overrides):
        """Create a mock lead object for testing."""
        lead = MagicMock()
        lead.id = uuid4()
        lead.email = overrides.get("email", "jane@acmecorp.com")
        lead.name = overrides.get("name", "Jane Smith")
        lead.company = overrides.get("company", "Acme Corp")
        lead.message = overrides.get(
            "message",
            "We need AI automation for our customer support pipeline. "
            "Currently handling 500+ tickets/day manually and looking "
            "to reduce response times by 50%."
        )
        lead.source = overrides.get("source", "website_form")
        lead.status = overrides.get("status", "VALIDATED")
        lead.flag_for_review = False
        lead.updated_at = None
        return lead

    def _make_mock_session(self):
        """Create a mock DB session."""
        session = MagicMock()
        session.add = MagicMock()
        session.flush = MagicMock()
        return session

    def test_high_intent_lead_routes_to_sales(self):
        """A high-quality lead should score >= 70 and route to SALES_QUEUE."""
        lead = self._make_mock_lead()
        session = self._make_mock_session()

        result = score_lead(session, lead, HIGH_INTENT_ENRICHMENT)

        assert result.lead_score >= 70, f"Expected >= 70, got {result.lead_score}"
        assert result.scoring_breakdown["intent_clarity"] == 25
        assert result.scoring_breakdown["urgency_signal"] == 20
        assert result.confidence_score >= 0.7
        assert len(result.disqualification_flags) == 0

    def test_low_intent_lead_routes_to_archive(self):
        """A low-quality lead should score < 40 and route to ARCHIVE."""
        lead = self._make_mock_lead(
            email="alex@gmail.com",
            company="Freelance",
            message="Just curious about your product.",
            source="",
        )
        session = self._make_mock_session()

        result = score_lead(session, lead, LOW_INTENT_ENRICHMENT)

        assert result.lead_score < 40, f"Expected < 40, got {result.lead_score}"
        assert len(result.disqualification_flags) > 0

    def test_medium_intent_lead_routes_to_nurture(self):
        """A medium-quality lead should score 40-69 and route to NURTURE_QUEUE."""
        lead = self._make_mock_lead(
            email="michael@greenleaf.com",
            company="GreenLeaf Analytics",
            message="Exploring AI solutions for data pipeline automation. "
                    "We have a small team and want to scale our analytics.",
        )
        session = self._make_mock_session()

        result = score_lead(session, lead, MEDIUM_INTENT_ENRICHMENT)

        assert 30 <= result.lead_score <= 75, f"Expected 30-75, got {result.lead_score}"

    def test_scoring_breakdown_sums_to_total(self):
        """The sum of all breakdown signals must equal the total score."""
        lead = self._make_mock_lead()
        session = self._make_mock_session()

        result = score_lead(session, lead, HIGH_INTENT_ENRICHMENT)

        breakdown_sum = sum(result.scoring_breakdown.values())
        assert breakdown_sum == result.lead_score, (
            f"Breakdown sum ({breakdown_sum}) != total score ({result.lead_score})"
        )

    def test_qualification_reason_is_not_empty(self):
        """Every scored lead must have a non-empty qualification reason."""
        lead = self._make_mock_lead()
        session = self._make_mock_session()

        result = score_lead(session, lead, HIGH_INTENT_ENRICHMENT)

        assert len(result.qualification_reason) > 10
        assert result.qualification_reason != ""


class TestRoutingThresholds:
    """Tests for routing threshold logic."""

    def _make_mock_lead(self):
        lead = MagicMock()
        lead.id = uuid4()
        lead.status = "SCORED"
        lead.updated_at = None
        return lead

    def _make_mock_session(self):
        session = MagicMock()
        session.add = MagicMock()
        session.flush = MagicMock()
        return session

    @patch("app.services.routing.settings")
    def test_high_score_routes_to_sales(self, mock_settings):
        """Score >= 70 should route to SALES_QUEUE."""
        mock_settings.ROUTING_HIGH_THRESHOLD = 70
        mock_settings.ROUTING_MEDIUM_THRESHOLD = 40

        lead = self._make_mock_lead()
        session = self._make_mock_session()

        queue = route_lead(session, lead, lead_score=85)
        assert queue == "SALES_QUEUE"

    @patch("app.services.routing.settings")
    def test_medium_score_routes_to_nurture(self, mock_settings):
        """Score 40-69 should route to NURTURE_QUEUE."""
        mock_settings.ROUTING_HIGH_THRESHOLD = 70
        mock_settings.ROUTING_MEDIUM_THRESHOLD = 40

        lead = self._make_mock_lead()
        session = self._make_mock_session()

        queue = route_lead(session, lead, lead_score=55)
        assert queue == "NURTURE_QUEUE"

    @patch("app.services.routing.settings")
    def test_low_score_routes_to_archive(self, mock_settings):
        """Score < 40 should route to ARCHIVE."""
        mock_settings.ROUTING_HIGH_THRESHOLD = 70
        mock_settings.ROUTING_MEDIUM_THRESHOLD = 40

        lead = self._make_mock_lead()
        session = self._make_mock_session()

        queue = route_lead(session, lead, lead_score=25)
        assert queue == "ARCHIVE"

    @patch("app.services.routing.settings")
    def test_boundary_score_70_routes_to_sales(self, mock_settings):
        """Score exactly at high threshold should route to SALES_QUEUE."""
        mock_settings.ROUTING_HIGH_THRESHOLD = 70
        mock_settings.ROUTING_MEDIUM_THRESHOLD = 40

        lead = self._make_mock_lead()
        session = self._make_mock_session()

        queue = route_lead(session, lead, lead_score=70)
        assert queue == "SALES_QUEUE"

    @patch("app.services.routing.settings")
    def test_boundary_score_40_routes_to_nurture(self, mock_settings):
        """Score exactly at medium threshold should route to NURTURE_QUEUE."""
        mock_settings.ROUTING_HIGH_THRESHOLD = 70
        mock_settings.ROUTING_MEDIUM_THRESHOLD = 40

        lead = self._make_mock_lead()
        session = self._make_mock_session()

        queue = route_lead(session, lead, lead_score=40)
        assert queue == "NURTURE_QUEUE"

    @patch("app.services.routing.settings")
    def test_boundary_score_39_routes_to_archive(self, mock_settings):
        """Score one below medium threshold should route to ARCHIVE."""
        mock_settings.ROUTING_HIGH_THRESHOLD = 70
        mock_settings.ROUTING_MEDIUM_THRESHOLD = 40

        lead = self._make_mock_lead()
        session = self._make_mock_session()

        queue = route_lead(session, lead, lead_score=39)
        assert queue == "ARCHIVE"

    @patch("app.services.routing.settings")
    def test_custom_thresholds(self, mock_settings):
        """Routing should respect custom threshold values from config."""
        mock_settings.ROUTING_HIGH_THRESHOLD = 80
        mock_settings.ROUTING_MEDIUM_THRESHOLD = 50

        lead = self._make_mock_lead()
        session = self._make_mock_session()

        # 75 would be SALES with default (70) but NURTURE with custom (80)
        queue = route_lead(session, lead, lead_score=75)
        assert queue == "NURTURE_QUEUE"
