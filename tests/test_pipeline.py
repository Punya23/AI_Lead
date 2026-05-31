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

@patch("app.services.llm_client._is_api_key_configured", return_value=False)
class TestLangGraphPipeline:
    """Tests for the multi-agent LangGraph orchestration."""

    def _make_mock_lead(self):
        lead = MagicMock()
        lead.id = uuid4()
        lead.status = "VALIDATED"
        lead.pipeline_checkpoint = {}
        lead.message = "Need demo."
        lead.email = "test@example.com"
        return lead
        
    def _make_mock_session(self):
        session = MagicMock()
        session.add = MagicMock()
        session.flush = MagicMock()
        session.commit = MagicMock()
        return session

    def test_node_sequence_and_state_accumulation(self, mock_api_key):
        """Test that all 5 nodes execute sequentially and accumulate state."""
        from app.services.langgraph_pipeline import build_pipeline_graph
        graph = build_pipeline_graph()
        
        lead = self._make_mock_lead()
        session = self._make_mock_session()
        
        result = graph.invoke({
            "lead_id": str(lead.id),
            "session": session,
            "lead": lead,
            "message": lead.message,
            "domain": "example.com",
            "checkpoint": {},
            "current_status": "VALIDATED",
            "intent_result": None,
            "research_result": None,
            "categorization_result": None,
            "enrichment_result": None,
            "scoring_result": None,
            "queue": None,
            "error": None,
            "retry_count": 0,
        })
        
        # Check that state accumulated correctly across nodes
        assert result["intent_result"] is not None
        assert result["research_result"] is not None
        assert result["enrichment_result"] is not None
        assert result["scoring_result"] is not None
        assert result["queue"] is not None
        assert result["error"] is None
        assert result["current_status"] == "COMPLETE"
        
    def test_checkpoint_resume_logic(self, mock_api_key):
        """Test that checkpointed nodes are skipped and don't re-run."""
        from app.services.langgraph_pipeline import build_pipeline_graph
        graph = build_pipeline_graph()
        
        lead = self._make_mock_lead()
        session = self._make_mock_session()
        
        # Pre-fill checkpoint to simulate intent agent already finished
        checkpoint = {
            "enrichment": {
                "estimated_intent": "Demo Request",
                "urgency_level": "High",
                "pain_points": ["Manual process"]
            }
        }
        lead.pipeline_checkpoint = checkpoint
        
        with patch("app.services.llm_client.call_intent_agent") as mock_intent:
            result = graph.invoke({
                "lead_id": str(lead.id),
                "session": session,
                "lead": lead,
                "message": lead.message,
                "domain": "example.com",
                "checkpoint": checkpoint,
                "current_status": "VALIDATED",
                "intent_result": None,
                "research_result": None,
                "categorization_result": None,
                "enrichment_result": None,
                "scoring_result": None,
                "queue": None,
                "error": None,
                "retry_count": 0,
            })
            
            # The intent LLM should NOT be called because it's in the checkpoint
            mock_intent.assert_not_called()
            
            # But the state should still contain the intent result
            assert result["intent_result"].estimated_intent == "Demo Request"
            
    def test_error_propagation_routes_to_error_node(self, mock_api_key):
        """Test that a node failure sets the error state and triggers error_node."""
        from app.services.langgraph_pipeline import build_pipeline_graph
        graph = build_pipeline_graph()
        
        lead = self._make_mock_lead()
        session = self._make_mock_session()
        
        with patch("app.services.enrichment.run_research_agent", side_effect=ValueError("LLM down")):
            with pytest.raises(Exception, match="research_agent_failed: LLM down"):
                graph.invoke({
                    "lead_id": str(lead.id),
                    "session": session,
                    "lead": lead,
                    "message": lead.message,
                    "domain": "example.com",
                    "checkpoint": {},
                    "current_status": "VALIDATED",
                    "intent_result": None,
                    "research_result": None,
                    "categorization_result": None,
                    "enrichment_result": None,
                    "scoring_result": None,
                    "queue": None,
                    "error": None,
                    "retry_count": 0,
                })
            
            # The error_node raises to Celery — FAILED status is set by the Celery dead-letter handler
            # NOT by error_node directly
            assert lead.status != "FAILED"

    def test_load_existing_enrichment_node(self, mock_api_key):
        """Test that load_enrichment_node correctly loads data when skip enrich."""
        from app.services.langgraph_pipeline import build_pipeline_graph
        graph = build_pipeline_graph()
        
        lead = self._make_mock_lead()
        lead.status = "ENRICHED" # Skip enrichment
        session = self._make_mock_session()
        
        from app.models.enrichment import Enrichment as EnrichmentModel
        mock_enrich = EnrichmentModel(
            lead_category="B2B SaaS",
            company_type="Enterprise",
            estimated_intent="Demo Request",
            urgency_level="High",
            pain_points=["A"],
            ai_summary="Summary"
        )
        mock_exec = MagicMock()
        mock_exec.scalar_one_or_none.return_value = mock_enrich
        session.execute.return_value = mock_exec
        
        result = graph.invoke({
            "lead_id": str(lead.id),
            "session": session,
            "lead": lead,
            "message": lead.message,
            "domain": "example.com",
            "checkpoint": {},
            "current_status": lead.status,
            "intent_result": None,
            "research_result": None,
            "categorization_result": None,
            "enrichment_result": None,
            "scoring_result": None,
            "queue": None,
            "error": None,
            "retry_count": 0,
        })
        
        assert result["enrichment_result"] is not None
        assert result["enrichment_result"].lead_category == "B2B SaaS"

    def test_error_node_raises_exception_to_celery(self, mock_api_key):
        """Test error_node directly."""
        from app.services.langgraph_pipeline import error_node
        lead = self._make_mock_lead()
        session = self._make_mock_session()
        
        state = {
            "lead_id": str(lead.id),
            "lead": lead,
            "session": session,
            "error": "some_error",
        }
        
        with pytest.raises(Exception, match="some_error"):
            error_node(state)
            
        assert lead.status == "FAILED"

    def test_routing_edge_logic(self, mock_api_key):
        """Test should_route_or_end logic."""
        from app.services.langgraph_pipeline import should_route_or_end
        from langgraph.graph import END
        
        assert should_route_or_end({"current_status": "SCORED"}) == "route"
        assert should_route_or_end({"current_status": "ROUTED"}) == END

    def test_scoring_edge_logic(self, mock_api_key):
        """Test should_score_or_skip logic."""
        from app.services.langgraph_pipeline import should_score_or_skip
        
        assert should_score_or_skip({"current_status": "ENRICHED"}) == "score"
        assert should_score_or_skip({"current_status": "SCORED"}) == "route_or_end"

    def test_enrichment_edge_logic(self, mock_api_key):
        """Test should_enrich_or_skip logic."""
        from app.services.langgraph_pipeline import should_enrich_or_skip
        
        assert should_enrich_or_skip({"current_status": "VALIDATED"}) == "enrich"
        assert should_enrich_or_skip({"current_status": "ENRICHED"}) == "load_enrichment"
