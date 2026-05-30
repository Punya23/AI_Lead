"""
ExecutionLog ORM model — audit trail for every pipeline stage.

Every stage transition (STARTED, SUCCESS, FAILED, RETRYING) is logged
with duration, attempt number, and full error context. This table is
the primary debugging tool for investigating pipeline failures.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class ExecutionLog(Base):
    """Per-stage execution log — the pipeline's audit trail."""

    __tablename__ = "execution_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )

    # --- Execution context ---
    stage: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True,
        comment="validation|enrichment|scoring|routing"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False,
        comment="STARTED|SUCCESS|FAILED|RETRYING"
    )
    attempt_number: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1
    )

    # --- Error context (populated on failure) ---
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_traceback: Mapped[str | None] = mapped_column(Text, nullable=True)

    # --- Performance ---
    duration_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="Stage execution time in milliseconds"
    )

    # --- Timestamps ---
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    # --- Relationships ---
    lead = relationship("Lead", back_populates="execution_logs")

    def __repr__(self) -> str:
        return f"<ExecutionLog lead_id={self.lead_id} stage={self.stage} status={self.status}>"
