"""Initial migration — create all 5 tables.

Revision ID: 001_initial
Revises: None
Create Date: 2025-05-30
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- leads ---
    op.create_table(
        "leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=False),
        sa.Column("email", sa.String(255), nullable=False, index=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("company", sa.String(255), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("source", sa.String(100), nullable=True),
        sa.Column("payload_hash", sa.String(100), nullable=False, unique=True, index=True),
        sa.Column("status", sa.String(50), nullable=False, server_default="RECEIVED", index=True),
        sa.Column("failure_reason", sa.String(500), nullable=True),
        sa.Column("flag_for_review", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("flag_reason", sa.Text(), nullable=True),
        sa.Column("pipeline_checkpoint", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"),
        sa.Column("dead_lettered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # --- enrichments ---
    op.create_table(
        "enrichments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, unique=True, index=True),
        sa.Column("lead_category", sa.String(50), nullable=False),
        sa.Column("company_type", sa.String(100), nullable=False),
        sa.Column("estimated_intent", sa.String(50), nullable=False),
        sa.Column("urgency_level", sa.String(20), nullable=False),
        sa.Column("pain_points", postgresql.JSONB(), nullable=False),
        sa.Column("ai_summary", sa.Text(), nullable=False),
        sa.Column("raw_llm_response", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # --- scores ---
    op.create_table(
        "scores",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, unique=True, index=True),
        sa.Column("lead_score", sa.Integer(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("qualification_reason", sa.Text(), nullable=False),
        sa.Column("disqualification_flags", postgresql.JSONB(), nullable=False),
        sa.Column("scoring_breakdown", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # --- routing_decisions ---
    op.create_table(
        "routing_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, unique=True, index=True),
        sa.Column("queue", sa.String(50), nullable=False),
        sa.Column("routing_reason", sa.Text(), nullable=False),
        sa.Column("score_at_routing", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    # --- execution_logs ---
    op.create_table(
        "execution_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("stage", sa.String(50), nullable=False, index=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_traceback", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("execution_logs")
    op.drop_table("routing_decisions")
    op.drop_table("scores")
    op.drop_table("enrichments")
    op.drop_table("leads")
