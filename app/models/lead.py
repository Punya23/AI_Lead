"""
Lead ORM model — core table for all incoming leads.

Stores raw payload (never modified), pipeline state, and review flags.
The payload_hash column (UNIQUE) enables content-based deduplication.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class Lead(Base):
    """Core lead table — tracks a lead from intake through routing."""

    __tablename__ = "leads"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # --- Original input (never modified after creation) ---
    raw_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)

    # --- Extracted fields (for indexing and querying) ---
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    company: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # --- Deduplication ---
    payload_hash: Mapped[str] = mapped_column(
        String(100), unique=True, nullable=False, index=True,
        comment="SHA-256 of email+company+message for dedup (64 chars + optional rejection suffix)"
    )

    # --- Pipeline state ---
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="RECEIVED", index=True,
        comment="RECEIVED|VALIDATED|ENRICHED|SCORED|ROUTED|COMPLETE|FAILED|REJECTED"
    )
    failure_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    pipeline_checkpoint: Mapped[dict | None] = mapped_column(
        JSONB, nullable=True, default={},
        comment="Lightweight LangGraph state checkpoint for node-level resume"
    )

    # --- Review flags ---
    flag_for_review: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    flag_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    dead_lettered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- Timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # --- Relationships ---
    enrichment = relationship("Enrichment", back_populates="lead", uselist=False, lazy="joined")
    score = relationship("Score", back_populates="lead", uselist=False, lazy="joined")
    routing_decision = relationship("RoutingDecision", back_populates="lead", uselist=False, lazy="joined")
    execution_logs = relationship("ExecutionLog", back_populates="lead", order_by="ExecutionLog.created_at", lazy="selectin")

    def __repr__(self) -> str:
        return f"<Lead id={self.id} email={self.email} status={self.status}>"
