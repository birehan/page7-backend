"""LLM and content AI (Phase 7)

Creates ai_decisions, ai_feedback, and ai_usage_counters (architecture/02 §8/§15).
Adds the deferred FKs from posts, post_versions, media_assets, and strategies
onto ai_decisions. runs/run_events already exist (Phase 3) and are untouched.

Revision ID: 0008_llm_content_ai
Revises: 0007_posts_workflow
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_llm_content_ai"
down_revision: str | None = "0007_posts_workflow"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_KIND_VALUES = (
    "captions",
    "plan",
    "plan_commit",
    "brand_generate",
    "brand_relearn",
    "strategy",
    "alt_text",
    "image_generate",
    "reply_suggest",
    "sentiment",
    "insight",
)
_PROVIDER_VALUES = ("openai", "anthropic", "fal", "internal")
_STATUS_VALUES = ("succeeded", "failed", "partial")


def upgrade() -> None:
    op.create_table(
        "ai_decisions",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.Text(), nullable=False),
        sa.Column("inputs_hash", sa.Text(), nullable=False),
        sa.Column(
            "input_summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("risk_score", sa.Numeric(4, 3), nullable=True),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("brand_version", sa.Integer(), nullable=True),
        sa.Column("parent_decision_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("provider_request_id", sa.Text(), nullable=True),
        sa.Column(
            "provider_response",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN (" + ", ".join(f"'{v}'" for v in _KIND_VALUES) + ")",
            name=op.f("ck_ai_decisions_kind_valid"),
        ),
        sa.CheckConstraint(
            "provider IN (" + ", ".join(f"'{v}'" for v in _PROVIDER_VALUES) + ")",
            name=op.f("ck_ai_decisions_provider_valid"),
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _STATUS_VALUES) + ")",
            name=op.f("ck_ai_decisions_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_ai_decisions_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["brand_id"],
            ["brands.id"],
            name=op.f("fk_ai_decisions_brand_id_brands"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_ai_decisions_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_decision_id"],
            ["ai_decisions.id"],
            name=op.f("fk_ai_decisions_parent_decision_id_ai_decisions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["brand_id", "brand_version"],
            ["brand_versions.brand_id", "brand_versions.version"],
            name="fk_ai_decisions_brand_version",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_decisions")),
    )
    op.create_index(
        "ix_aidec_org_created",
        "ai_decisions",
        ["organization_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
    )
    op.create_index("ix_aidec_inputs_hash", "ai_decisions", ["inputs_hash"])
    op.create_index("ix_aidec_target", "ai_decisions", ["target_type", "target_id"])

    op.create_table(
        "ai_feedback",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("variant_index", sa.SmallInteger(), nullable=False),
        sa.Column("rating", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "rating IN ('up', 'down')",
            name=op.f("ck_ai_feedback_rating_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_ai_feedback_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["ai_decisions.id"],
            name=op.f("fk_ai_feedback_decision_id_ai_decisions"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_ai_feedback_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_feedback")),
        sa.UniqueConstraint(
            "decision_id",
            "variant_index",
            "user_id",
            name="uq_ai_feedback_decision_variant_user",
        ),
    )

    op.create_table(
        "ai_usage_counters",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("generations", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("prompt_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "completion_tokens",
            sa.BigInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "cost_usd",
            sa.Numeric(14, 6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_ai_usage_counters_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "period_month",
            name=op.f("pk_ai_usage_counters"),
        ),
    )

    # Close forward references left bare by Phases 4–6.
    op.create_foreign_key(
        op.f("fk_posts_ai_decision_id_ai_decisions"),
        "posts",
        "ai_decisions",
        ["ai_decision_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_post_versions_ai_decision_id_ai_decisions"),
        "post_versions",
        "ai_decisions",
        ["ai_decision_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_media_assets_ai_decision_id_ai_decisions"),
        "media_assets",
        "ai_decisions",
        ["ai_decision_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_strategies_generated_by_decision_id_ai_decisions"),
        "strategies",
        "ai_decisions",
        ["generated_by_decision_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_strategies_generated_by_decision_id_ai_decisions"),
        "strategies",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_media_assets_ai_decision_id_ai_decisions"),
        "media_assets",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_post_versions_ai_decision_id_ai_decisions"),
        "post_versions",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_posts_ai_decision_id_ai_decisions"),
        "posts",
        type_="foreignkey",
    )
    op.drop_table("ai_usage_counters")
    op.drop_table("ai_feedback")
    op.drop_index("ix_aidec_target", table_name="ai_decisions")
    op.drop_index("ix_aidec_inputs_hash", table_name="ai_decisions")
    op.drop_index("ix_aidec_org_created", table_name="ai_decisions")
    op.drop_table("ai_decisions")
