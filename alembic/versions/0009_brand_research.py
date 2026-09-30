"""Brand research runs (Phase 8)

Creates brand_research_runs (architecture/02 §8) with the five-value status
CHECK including `partial` from the start (architecture/08 §4 amendment).

Revision ID: 0009_brand_research
Revises: 0008_llm_content_ai
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009_brand_research"
down_revision: str | None = "0008_llm_content_ai"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_STATUS_VALUES = ("queued", "running", "succeeded", "partial", "failed")


def upgrade() -> None:
    op.create_table(
        "brand_research_runs",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("crawled_pages", sa.Integer(), nullable=True),
        sa.Column(
            "extracted",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "proposal",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_brand_version", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'partial', 'failed')",
            name=op.f("ck_brand_research_runs_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_brand_research_runs_brand_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_brand_research_runs_requested_by_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["ai_decisions.id"],
            name=op.f("fk_brand_research_runs_decision_id_ai_decisions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brand_research_runs")),
    )
    op.create_index(
        "ix_brand_research_runs_brand_created",
        "brand_research_runs",
        ["brand_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_brand_research_runs_brand_source",
        "brand_research_runs",
        ["brand_id", "source_url"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_brand_research_runs_brand_source", table_name="brand_research_runs")
    op.drop_index("ix_brand_research_runs_brand_created", table_name="brand_research_runs")
    op.drop_table("brand_research_runs")
