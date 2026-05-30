"""
Pydantic schemas for AI enrichment — used to validate LLM output.

The EnrichmentResult schema is passed to Gemini as response_schema,
guaranteeing structured JSON output. Any response that doesn't match
this schema triggers a retry with a corrective prompt.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class EnrichmentResult(BaseModel):
    """Schema for validated LLM enrichment output.

    This is used both as the Gemini response_schema AND as the
    Pydantic validator for the LLM response. Double duty.
    """

    lead_category: str = Field(
        ...,
        description="One of: B2B SaaS, Enterprise, SMB, Startup, Individual, Unknown"
    )
    company_type: str = Field(
        ...,
        description="Type of company (e.g., Customer Support Platform, Fintech, E-commerce)"
    )
    estimated_intent: str = Field(
        ...,
        description="One of: Demo Request, Partnership, Technical Inquiry, Pricing, Spam, Unknown"
    )
    urgency_level: str = Field(
        ...,
        description="One of: High, Medium, Low"
    )
    pain_points: list[str] = Field(
        ...,
        max_length=5,
        description="List of identified pain points (max 5 items)"
    )
    ai_summary: str = Field(
        ...,
        max_length=500,
        description="2-3 sentence summary of the lead's needs (max 150 words)"
    )


class EnrichmentResponse(BaseModel):
    """API response schema for enrichment data."""

    id: UUID
    lead_id: UUID
    lead_category: str
    company_type: str
    estimated_intent: str
    urgency_level: str
    pain_points: list[str]
    ai_summary: str
    created_at: datetime

    model_config = {"from_attributes": True}
