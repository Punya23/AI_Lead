"""
Score ORM model — stores deterministic scoring results.

Scoring is done by Python code (not LLM) using weighted signals.
The scoring_breakdown JSONB column shows exactly how points were awarded,
making the score fully explainable and auditable.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Score(Base):
    """Deterministic scoring result with full signal breakdown."""

    __tablename__ = "scores"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )

    # --- Score results ---
    lead_score: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="0-100 weighted score"
    )
    confidence_score: Mapped[float] = mapped_column(
        Float, nullable=False,
        comment="0.0-1.0 confidence level"
    )
    qualification_reason: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Template-generated explanation of the score"
    )
    disqualification_flags: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list,
        comment="List of reasons that reduced the score"
    )

    # --- Explainability ---
    scoring_breakdown: Mapped[dict] = mapped_column(
        JSONB, nullable=False,
        comment="Points per signal: intent_clarity, urgency_signal, etc."
    )

    # --- Timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    # --- Relationships ---
    lead = relationship("Lead", back_populates="score")

    def __repr__(self) -> str:
        return f"<Score lead_id={self.lead_id} score={self.lead_score}>"
