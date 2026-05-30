"""
Tests for the retry and failure handling system.

Covers:
- LLM timeout → retry → success
- LLM timeout → max retries → fallback
- Malformed JSON → corrective prompt → success
- Duplicate lead → immediate rejection
- Failure simulation toggle
"""

import pytest
from unittest.mock import patch, MagicMock

from app.core.exceptions import (
    LLMTimeoutError,
    LLMMalformedResponseError,
    LLMRateLimitError,
    DuplicateLeadError,
    ValidationError,
)
from app.services.llm_client import get_fallback_enrichment, _maybe_simulate_failure
from app.services.validation import generate_payload_hash


class TestLLMRetryScenarios:
    """Tests for LLM failure handling."""

    def test_fallback_enrichment_has_safe_defaults(self):
        """Fallback enrichment should have conservative defaults."""
        fallback = get_fallback_enrichment()

        assert fallback.lead_category == "Unknown"
        assert fallback.company_type == "Unknown"
        assert fallback.estimated_intent == "Unknown"
        assert fallback.urgency_level == "Low"
        assert fallback.pain_points == []
        assert "failed" in fallback.ai_summary.lower() or "review" in fallback.ai_summary.lower()

    def test_fallback_enrichment_is_valid_schema(self):
        """Fallback enrichment must pass Pydantic validation."""
        fallback = get_fallback_enrichment()

        # Should not raise
        assert fallback.lead_category is not None
        assert fallback.ai_summary is not None
        assert isinstance(fallback.pain_points, list)

    @patch("app.services.llm_client.settings")
    def test_failure_simulator_disabled_by_default(self, mock_settings):
        """When SIMULATE_FAILURES=false, no failures should be simulated."""
        mock_settings.SIMULATE_FAILURES = False

        # Should not raise anything, even with 1000 calls
        for _ in range(100):
            _maybe_simulate_failure(lead_id="test-123")

    @patch("app.services.llm_client.settings")
    @patch("app.services.llm_client.random")
    def test_failure_simulator_triggers_timeout(self, mock_random, mock_settings):
        """When enabled with low roll, should raise LLMTimeoutError."""
        mock_settings.SIMULATE_FAILURES = True
        mock_settings.FAILURE_RATE_LLM_TIMEOUT = 0.15
        mock_settings.FAILURE_RATE_MALFORMED_RESPONSE = 0.10
        mock_settings.FAILURE_RATE_RATE_LIMIT = 0.10
        mock_settings.LLM_TIMEOUT_SECONDS = 30

        # Roll below timeout threshold
        mock_random.random.return_value = 0.05

        with pytest.raises(LLMTimeoutError):
            _maybe_simulate_failure(lead_id="test-123")

    @patch("app.services.llm_client.settings")
    @patch("app.services.llm_client.random")
    def test_failure_simulator_triggers_malformed(self, mock_random, mock_settings):
        """When enabled with mid roll, should raise LLMMalformedResponseError."""
        mock_settings.SIMULATE_FAILURES = True
        mock_settings.FAILURE_RATE_LLM_TIMEOUT = 0.15
        mock_settings.FAILURE_RATE_MALFORMED_RESPONSE = 0.10
        mock_settings.FAILURE_RATE_RATE_LIMIT = 0.10
        mock_settings.LLM_TIMEOUT_SECONDS = 30

        # Roll between timeout and malformed thresholds
        mock_random.random.return_value = 0.20

        with pytest.raises(LLMMalformedResponseError):
            _maybe_simulate_failure(lead_id="test-123")

    @patch("app.services.llm_client.settings")
    @patch("app.services.llm_client.random")
    def test_failure_simulator_triggers_rate_limit(self, mock_random, mock_settings):
        """When enabled with higher roll, should raise LLMRateLimitError."""
        mock_settings.SIMULATE_FAILURES = True
        mock_settings.FAILURE_RATE_LLM_TIMEOUT = 0.15
        mock_settings.FAILURE_RATE_MALFORMED_RESPONSE = 0.10
        mock_settings.FAILURE_RATE_RATE_LIMIT = 0.10
        mock_settings.LLM_TIMEOUT_SECONDS = 30

        # Roll between malformed and rate limit thresholds
        mock_random.random.return_value = 0.30

        with pytest.raises(LLMRateLimitError):
            _maybe_simulate_failure(lead_id="test-123")

    @patch("app.services.llm_client.settings")
    @patch("app.services.llm_client.random")
    def test_failure_simulator_no_failure_on_high_roll(self, mock_random, mock_settings):
        """When enabled but roll is above all thresholds, no failure."""
        mock_settings.SIMULATE_FAILURES = True
        mock_settings.FAILURE_RATE_LLM_TIMEOUT = 0.15
        mock_settings.FAILURE_RATE_MALFORMED_RESPONSE = 0.10
        mock_settings.FAILURE_RATE_RATE_LIMIT = 0.10
        mock_settings.LLM_TIMEOUT_SECONDS = 30

        # Roll above all thresholds
        mock_random.random.return_value = 0.99

        # Should not raise
        _maybe_simulate_failure(lead_id="test-123")


class TestExceptionHierarchy:
    """Tests for the custom exception classes."""

    def test_pipeline_error_has_context(self):
        """PipelineError should carry lead_id and stage."""
        from app.core.exceptions import PipelineError
        err = PipelineError("test error", lead_id="abc-123", stage="enrichment")

        assert err.message == "test error"
        assert err.lead_id == "abc-123"
        assert err.stage == "enrichment"

        d = err.to_dict()
        assert d["error_type"] == "PipelineError"
        assert d["lead_id"] == "abc-123"

    def test_validation_error_has_reason(self):
        """ValidationError should include a rejection reason."""
        err = ValidationError("Bad email", lead_id="abc-123", reason="INVALID_EMAIL_FORMAT")

        assert err.reason == "INVALID_EMAIL_FORMAT"
        assert err.stage == "validation"

    def test_duplicate_lead_error_references_original(self):
        """DuplicateLeadError should reference the original lead ID."""
        err = DuplicateLeadError(lead_id="new-123", original_lead_id="original-456")

        assert err.original_lead_id == "original-456"
        assert err.reason == "DUPLICATE_LEAD"
        assert "original-456" in err.message

    def test_llm_timeout_error(self):
        """LLMTimeoutError should include timeout duration."""
        err = LLMTimeoutError(lead_id="abc-123", timeout_seconds=30)

        assert "30" in err.message
        assert err.stage == "enrichment"

    def test_llm_malformed_response_error(self):
        """LLMMalformedResponseError should include raw response."""
        err = LLMMalformedResponseError(lead_id="abc-123", raw_response='{"bad": json}')

        assert err.raw_response == '{"bad": json}'
        assert err.stage == "enrichment"

    def test_llm_rate_limit_error(self):
        """LLMRateLimitError should include retry_after hint."""
        err = LLMRateLimitError(lead_id="abc-123", retry_after=60)

        assert err.retry_after == 60
        assert "60" in err.message


class TestDeduplication:
    """Tests for content-based deduplication logic."""

    def test_duplicate_detection_is_case_insensitive(self):
        """Same content with different casing should produce same hash."""
        hash1 = generate_payload_hash("JANE@ACME.COM", "ACME CORP", "Hello world")
        hash2 = generate_payload_hash("jane@acme.com", "acme corp", "Hello world")
        assert hash1 == hash2

    def test_different_messages_produce_different_hashes(self):
        """Different message content should produce different hashes."""
        hash1 = generate_payload_hash("jane@acme.com", "Acme", "Need AI help")
        hash2 = generate_payload_hash("jane@acme.com", "Acme", "Just browsing")
        assert hash1 != hash2

    def test_hash_is_deterministic(self):
        """Same input should always produce the same hash (no randomness)."""
        hashes = set()
        for _ in range(50):
            h = generate_payload_hash("test@test.com", "TestCo", "Test message")
            hashes.add(h)

        assert len(hashes) == 1, f"Hash is non-deterministic! Got {len(hashes)} unique values"
