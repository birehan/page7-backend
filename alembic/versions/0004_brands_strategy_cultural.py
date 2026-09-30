"""Brands, strategy, cultural calendar (Phase 4)

Creates brands + brand_versions + content_pillars + brand_competitors,
strategies, cultural_events + organization_cultural_event_settings
(architecture/02 §3/§12/§13), then backfills the two foreign keys Phase 2
could not add because `brands` did not exist yet:
  - oauth_states.brand_id → brands(id)
  - memberships.last_brand_id → brands(id) ON DELETE SET NULL

All tables are new and empty at deploy time, so no index needs CONCURRENTLY.

Revision ID: 0004_brands_strategy_cultural
Revises: 0003_background_jobs
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_brands_strategy_cultural"
down_revision: str | None = "0003_background_jobs"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # brands
    # ------------------------------------------------------------------
    op.create_table(
        "brands",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("industry", sa.Text(), server_default="", nullable=False),
        sa.Column("city", sa.Text(), server_default="", nullable=False),
        sa.Column("website", sa.Text(), nullable=True),
        sa.Column("logo_url", sa.Text(), nullable=True),
        sa.Column(
            "guidelines",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
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
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "jsonb_typeof(guidelines) = 'object'",
            name=op.f("ck_brands_guidelines_object"),
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by"],
            ["users.id"],
            name=op.f("fk_brands_deleted_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_brands_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brands")),
        sa.UniqueConstraint("id", "organization_id", name="uq_brands_id_organization_id"),
    )
    op.create_index(
        "ux_brands_org_name",
        "brands",
        ["organization_id", sa.literal_column("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # ------------------------------------------------------------------
    # brand_versions (append-only)
    # ------------------------------------------------------------------
    op.create_table(
        "brand_versions",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('generated', 'edited', 'ai_relearn', 'restored')",
            name=op.f("ck_brand_versions_reason_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["users.id"],
            name=op.f("fk_brand_versions_author_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["brand_id"],
            ["brands.id"],
            name=op.f("fk_brand_versions_brand_id_brands"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_brand_versions_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brand_versions")),
        sa.UniqueConstraint("brand_id", "version", name="uq_brand_versions_brand_id_version"),
    )

    # ------------------------------------------------------------------
    # content_pillars
    # ------------------------------------------------------------------
    op.create_table(
        "content_pillars",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "weight",
            sa.Numeric(5, 2),
            server_default="1",
            nullable=False,
        ),
        sa.Column("position", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
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
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint("weight >= 0", name=op.f("ck_content_pillars_weight_non_negative")),
        sa.ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_content_pillars_brand_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by"],
            ["users.id"],
            name=op.f("fk_content_pillars_deleted_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_pillars")),
        sa.UniqueConstraint("id", "brand_id", name="uq_content_pillars_id_brand_id"),
    )
    op.create_index(
        "ux_pillars_brand_name",
        "content_pillars",
        ["brand_id", sa.literal_column("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # ------------------------------------------------------------------
    # brand_competitors
    # ------------------------------------------------------------------
    op.create_table(
        "brand_competitors",
        sa.Column("handle", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
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
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_brand_competitors_brand_org",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_brand_competitors")),
    )
    op.create_index(
        "ux_brand_competitors_brand_platform_handle",
        "brand_competitors",
        ["brand_id", "platform", sa.literal_column("lower(handle)")],
        unique=True,
    )

    # ------------------------------------------------------------------
    # strategies
    # ------------------------------------------------------------------
    op.create_table(
        "strategies",
        sa.Column(
            "goals",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "cadence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        # Bare uuid until Phase 7 creates ai_decisions — no FK yet.
        sa.Column("generated_by_decision_id", sa.Uuid(), nullable=True),
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
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "jsonb_typeof(goals) = 'array'",
            name=op.f("ck_strategies_goals_array"),
        ),
        sa.ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_strategies_brand_org",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_strategies")),
        sa.UniqueConstraint("brand_id", name="uq_strategies_brand_id"),
    )

    # ------------------------------------------------------------------
    # cultural_events (global catalog)
    # ------------------------------------------------------------------
    op.create_table(
        "cultural_events",
        sa.Column("slug", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("name_ar", sa.Text(), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("hijri_year", sa.SmallInteger(), nullable=True),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column(
            "enabled_by_default",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
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
            "kind IN ('religious', 'national', 'seasonal')",
            name=op.f("ck_cultural_events_kind_valid"),
        ),
        sa.CheckConstraint(
            "source IN ('umm_al_qura', 'manual')",
            name=op.f("ck_cultural_events_source_valid"),
        ),
        sa.CheckConstraint(
            "end_date >= start_date",
            name=op.f("ck_cultural_events_end_after_start"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_cultural_events")),
        sa.UniqueConstraint("slug", name=op.f("uq_cultural_events_slug")),
    )
    op.create_index("ix_cultural_events_start", "cultural_events", ["start_date"])

    # ------------------------------------------------------------------
    # organization_cultural_event_settings
    # ------------------------------------------------------------------
    op.create_table(
        "organization_cultural_event_settings",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("cultural_event_id", sa.Uuid(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cultural_event_id"],
            ["cultural_events.id"],
            name=op.f("fk_organization_cultural_event_settings_cultural_event_id_cultural_events"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_organization_cultural_event_settings_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "organization_id",
            "cultural_event_id",
            name=op.f("pk_organization_cultural_event_settings"),
        ),
    )

    # ------------------------------------------------------------------
    # Forward-reference FKs Phase 2 left open
    # ------------------------------------------------------------------
    op.create_foreign_key(
        op.f("fk_oauth_states_brand_id_brands"),
        "oauth_states",
        "brands",
        ["brand_id"],
        ["id"],
    )
    op.create_foreign_key(
        op.f("fk_memberships_last_brand_id_brands"),
        "memberships",
        "brands",
        ["last_brand_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_memberships_last_brand_id_brands"), "memberships", type_="foreignkey"
    )
    op.drop_constraint(op.f("fk_oauth_states_brand_id_brands"), "oauth_states", type_="foreignkey")
    op.drop_table("organization_cultural_event_settings")
    op.drop_index("ix_cultural_events_start", table_name="cultural_events")
    op.drop_table("cultural_events")
    op.drop_table("strategies")
    op.drop_index("ux_brand_competitors_brand_platform_handle", table_name="brand_competitors")
    op.drop_table("brand_competitors")
    op.drop_index("ux_pillars_brand_name", table_name="content_pillars")
    op.drop_table("content_pillars")
    op.drop_table("brand_versions")
    op.drop_index("ux_brands_org_name", table_name="brands")
    op.drop_table("brands")
