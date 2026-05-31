"""
Pydantic schemas for AI enrichment — used to validate LLM output.

The EnrichmentResult schema is passed to Gemini as response_schema,
guaranteeing structured JSON output. Any response that doesn't match
this schema triggers a retry with a corrective prompt.
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class IntentUrgencyResult(BaseModel):
    """Output schema for the Enrichment Agent (Node 1)."""
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


class CompanyContextResult(BaseModel):
    """Output schema for the Research Agent (Node 2)."""
    company_type: str = Field(
        ...,
        description="Type of company (e.g., Customer Support Platform, Fintech, E-commerce)"
    )
    ai_summary: str = Field(
        ...,
        max_length=500,
        description="2-3 sentence summary of the lead's needs (max 150 words)"
    )


class CategorizationResult(BaseModel):
    """Output schema for the Categorization Agent (Node 3)."""
    lead_category: str = Field(
        ...,
        description="One of: B2B SaaS, Enterprise, SMB, Startup, Individual, Unknown"
    )


class EnrichmentResult(BaseModel):
    """Schema for validated LLM enrichment output.

    This represents the final combined output of all 3 agents,
    ready for database persistence.
    """
    lead_category: str
    company_type: str
    estimated_intent: str
    urgency_level: str
    pain_points: list[str]
    ai_summary: str

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
