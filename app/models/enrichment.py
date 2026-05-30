"""
Enrichment ORM model — stores AI enrichment outputs from Gemini.

Each enrichment is linked 1:1 to a lead. The raw_llm_response column
preserves the full LLM output for debugging and audit purposes.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Enrichment(Base):
    """AI enrichment output — category, intent, urgency, pain points, summary."""

    __tablename__ = "enrichments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )

    # --- Enrichment fields ---
    lead_category: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="B2B SaaS|Enterprise|SMB|Startup|Individual|Unknown"
    )
    company_type: Mapped[str] = mapped_column(String(100), nullable=False)
    estimated_intent: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="Demo Request|Partnership|Technical Inquiry|Pricing|Spam|Unknown"
    )
    urgency_level: Mapped[str] = mapped_column(
        String(20), nullable=False,
        comment="High|Medium|Low"
    )
    pain_points: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    ai_summary: Mapped[str] = mapped_column(Text, nullable=False)

    # --- Debugging ---
    raw_llm_response: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Full LLM output preserved for debugging"
    )

    # --- Timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    # --- Relationships ---
    lead = relationship("Lead", back_populates="enrichment")

    def __repr__(self) -> str:
        return f"<Enrichment lead_id={self.lead_id} intent={self.estimated_intent}>"
