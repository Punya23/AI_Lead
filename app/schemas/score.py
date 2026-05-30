"""
Pydantic schemas for the deterministic scoring engine.

ScoringResult is the output of the Python scoring function.
ScoreResponse is the API response schema.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ScoringResult(BaseModel):
    """Output of the deterministic scoring function.

    This is produced by Python code (not LLM). Same input always
    produces the same output — fully testable and auditable.
    """

    lead_score: int = Field(..., ge=0, le=100, description="Weighted score 0-100")
    confidence_score: float = Field(..., ge=0.0, le=1.0, description="Confidence 0.0-1.0")
    qualification_reason: str = Field(..., description="Template-generated explanation")
    disqualification_flags: list[str] = Field(
        default_factory=list,
        description="Reasons that reduced the score"
    )
    scoring_breakdown: dict = Field(
        ...,
        description="Points per signal: intent_clarity, urgency_signal, etc."
    )


class ScoreResponse(BaseModel):
    """API response schema for scoring data."""

    id: UUID
    lead_id: UUID
    lead_score: int
    confidence_score: float
    qualification_reason: str
    disqualification_flags: list[str]
    scoring_breakdown: dict
    created_at: datetime

    model_config = {"from_attributes": True}
