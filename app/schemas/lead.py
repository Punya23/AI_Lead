"""
Pydantic schemas for lead input/output validation.

These schemas validate API requests and structure API responses.
They are NOT the same as ORM models — they define the API contract.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, EmailStr, Field


# =============================================================================
# Request Schemas
# =============================================================================

class LeadCreateRequest(BaseModel):
    """Schema for POST /api/v1/leads — single lead submission."""

    name: str = Field(..., min_length=1, max_length=255, description="Full name of the lead")
    email: EmailStr = Field(..., description="Email address of the lead")
    company: str = Field(..., min_length=1, max_length=255, description="Company name")
    message: str = Field(..., min_length=1, description="Lead's message or inquiry")
    source: str | None = Field(None, max_length=100, description="Lead source (website_form, api, csv, etc.)")


# =============================================================================
# Response Schemas
# =============================================================================

class LeadResponse(BaseModel):
    """Response after submitting a lead — confirms receipt and queue status."""

    lead_id: UUID
    status: str
    message: str
    queued_at: datetime

    model_config = {"from_attributes": True}


class EnrichmentSummary(BaseModel):
    """Enrichment data included in lead detail response."""

    lead_category: str
    company_type: str
    estimated_intent: str
    urgency_level: str
    pain_points: list[str]
    ai_summary: str
    created_at: datetime

    model_config = {"from_attributes": True}


class ScoreSummary(BaseModel):
    """Score data included in lead detail response."""

    lead_score: int
    confidence_score: float
    qualification_reason: str
    disqualification_flags: list[str]
    scoring_breakdown: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class RoutingSummary(BaseModel):
    """Routing data included in lead detail response."""

    queue: str
    routing_reason: str
    score_at_routing: int
    created_at: datetime

    model_config = {"from_attributes": True}


class ExecutionLogEntry(BaseModel):
    """Single execution log entry in the lead timeline."""

    id: UUID
    stage: str
    status: str
    attempt_number: int
    error_message: str | None = None
    duration_ms: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class LeadDetailResponse(BaseModel):
    """Full lead detail — includes enrichment, score, routing, and timeline."""

    id: UUID
    email: str
    name: str
    company: str
    message: str
    source: str | None
    status: str
    failure_reason: str | None
    flag_for_review: bool
    created_at: datetime
    updated_at: datetime

    enrichment: EnrichmentSummary | None = None
    score: ScoreSummary | None = None
    routing_decision: RoutingSummary | None = None
    execution_logs: list[ExecutionLogEntry] = []

    model_config = {"from_attributes": True}


class LeadListItem(BaseModel):
    """Compact lead representation for list endpoints."""

    id: UUID
    email: str
    name: str
    company: str
    status: str
    flag_for_review: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class LeadListResponse(BaseModel):
    """Paginated list of leads."""

    leads: list[LeadListItem]
    total: int
    limit: int
    offset: int


# =============================================================================
# Batch Upload Schemas
# =============================================================================

class BatchRejection(BaseModel):
    """A single row that was rejected during CSV batch upload."""

    row: int
    email: str | None = None
    reason: str


class LeadBatchResponse(BaseModel):
    """Response after CSV batch upload."""

    total: int
    queued: int
    rejected: list[BatchRejection]
    lead_ids: list[str] = []
