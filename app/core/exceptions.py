"""
Custom exception hierarchy for the lead pipeline.

Every exception carries structured context for logging and debugging.
No bare exceptions — every error type has a specific handler.
"""


class PipelineError(Exception):
    """Base exception for all pipeline errors."""

    def __init__(self, message: str, lead_id: str | None = None, stage: str | None = None):
        self.message = message
        self.lead_id = lead_id
        self.stage = stage
        super().__init__(self.message)

    def to_dict(self) -> dict:
        """Serialize exception context for logging.

        Returns:
            dict: Structured error context.
        """
        return {
            "error_type": self.__class__.__name__,
            "message": self.message,
            "lead_id": self.lead_id,
            "stage": self.stage,
        }


class ValidationError(PipelineError):
    """Raised when lead validation fails (email, dedup, spam, etc.)."""

    def __init__(self, message: str, lead_id: str | None = None, reason: str = "UNKNOWN"):
        self.reason = reason
        super().__init__(message=message, lead_id=lead_id, stage="validation")


class DuplicateLeadError(ValidationError):
    """Raised when a duplicate lead is detected via payload hash."""

    def __init__(self, lead_id: str | None = None, original_lead_id: str | None = None):
        self.original_lead_id = original_lead_id
        super().__init__(
            message=f"Duplicate lead detected. Original: {original_lead_id}",
            lead_id=lead_id,
            reason="DUPLICATE_LEAD",
        )


class SpamDetectedError(ValidationError):
    """Raised when a lead is detected as spam or fake."""

    def __init__(self, lead_id: str | None = None, detail: str = ""):
        super().__init__(
            message=f"Spam detected: {detail}",
            lead_id=lead_id,
            reason=f"SPAM_DETECTED: {detail}",
        )


class LLMError(PipelineError):
    """Base exception for LLM-related failures."""

    def __init__(self, message: str, lead_id: str | None = None, provider: str = "gemini"):
        self.provider = provider
        super().__init__(message=message, lead_id=lead_id, stage="enrichment")


class LLMTimeoutError(LLMError):
    """Raised when an LLM call times out."""

    def __init__(self, lead_id: str | None = None, timeout_seconds: int = 30):
        super().__init__(
            message=f"LLM call timed out after {timeout_seconds}s",
            lead_id=lead_id,
        )


class LLMMalformedResponseError(LLMError):
    """Raised when the LLM returns invalid/unparseable JSON."""

    def __init__(self, lead_id: str | None = None, raw_response: str = ""):
        self.raw_response = raw_response
        super().__init__(
            message="LLM returned malformed response that failed schema validation",
            lead_id=lead_id,
        )


class LLMRateLimitError(LLMError):
    """Raised when the LLM API returns a rate limit error (429)."""

    def __init__(self, lead_id: str | None = None, retry_after: int | None = None):
        self.retry_after = retry_after
        super().__init__(
            message=f"LLM rate limited. Retry after: {retry_after}s",
            lead_id=lead_id,
        )


class EnrichmentError(PipelineError):
    """Raised when the enrichment stage fails after all retries."""

    def __init__(self, message: str, lead_id: str | None = None):
        super().__init__(message=message, lead_id=lead_id, stage="enrichment")


class ScoringError(PipelineError):
    """Raised when the scoring stage fails."""

    def __init__(self, message: str, lead_id: str | None = None):
        super().__init__(message=message, lead_id=lead_id, stage="scoring")


class RoutingError(PipelineError):
    """Raised when the routing stage fails."""

    def __init__(self, message: str, lead_id: str | None = None):
        super().__init__(message=message, lead_id=lead_id, stage="routing")
