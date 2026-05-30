"""
RoutingDecision ORM model — records where each lead was routed.

Routing is deterministic: score >= high_threshold → SALES_QUEUE,
score >= medium_threshold → NURTURE_QUEUE, else → ARCHIVE.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class RoutingDecision(Base):
    """Records the routing decision for a processed lead."""

    __tablename__ = "routing_decisions"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )

    # --- Routing result ---
    queue: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="SALES_QUEUE|NURTURE_QUEUE|ARCHIVE"
    )
    routing_reason: Mapped[str] = mapped_column(
        Text, nullable=False,
        comment="Explanation of why this queue was selected"
    )
    score_at_routing: Mapped[int] = mapped_column(
        Integer, nullable=False,
        comment="Score at the time routing was decided"
    )

    # --- Timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    # --- Relationships ---
    lead = relationship("Lead", back_populates="routing_decision")

    def __repr__(self) -> str:
        return f"<RoutingDecision lead_id={self.lead_id} queue={self.queue}>"
