"""Image generations and outputs (Phase 11)

Creates image_generations / image_generation_outputs (architecture/02 §8 as
amended by Phase 11: decision_id is nullable until the run finalizes) and closes
the deferred media_assets.image_generation_output_id FK.

Revision ID: 0013_image_generation
Revises: 0012_publications
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_image_generation"
down_revision: str | None = "0012_publications"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "image_generations",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by", sa.Uuid(), nullable=False),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column(
            "provider",
            sa.Text(),
            server_default="fal",
            nullable=False,
        ),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("negative_prompt", sa.Text(), nullable=True),
        sa.Column("style", sa.Text(), nullable=False),
        sa.Column("aspect", sa.Text(), nullable=False),
        sa.Column("count", sa.SmallInteger(), nullable=False),
        sa.Column(
            "use_brand_colors",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Text(),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("provider_request_id", sa.Text(), nullable=True),
        sa.Column("cost_usd", sa.Numeric(12, 6), nullable=True),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
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
            "style IN ('photo', 'flat', 'three-d', 'minimal', 'saudi-modern')",
            name=op.f("ck_image_generations_style_valid"),
        ),
        sa.CheckConstraint(
            "aspect IN ('square', 'portrait', 'vertical', 'landscape')",
            name=op.f("ck_image_generations_aspect_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'partial')",
            name=op.f("ck_image_generations_status_valid"),
        ),
        sa.CheckConstraint(
            "count BETWEEN 1 AND 4",
            name=op.f("ck_image_generations_count_range"),
        ),
        sa.ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_image_generations_brand_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_image_generations_requested_by_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["ai_decisions.id"],
            name=op.f("fk_image_generations_decision_id_ai_decisions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_image_generations")),
    )
    op.create_index(
        "ix_image_generations_brand_created",
        "image_generations",
        ["brand_id", sa.literal_column("created_at DESC")],
        unique=False,
    )

    op.create_table(
        "image_generation_outputs",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("image_generation_id", sa.Uuid(), nullable=False),
        sa.Column("index", sa.SmallInteger(), nullable=False),
        sa.Column("provider_url", sa.Text(), nullable=False),
        sa.Column("provider_url_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=True),
        sa.Column("r2_key", sa.Text(), nullable=True),
        sa.Column("kept_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "width > 0",
            name=op.f("ck_image_generation_outputs_width_positive"),
        ),
        sa.CheckConstraint(
            "height > 0",
            name=op.f("ck_image_generation_outputs_height_positive"),
        ),
        sa.CheckConstraint(
            '"index" >= 0',
            name=op.f("ck_image_generation_outputs_index_nonnegative"),
        ),
        sa.ForeignKeyConstraint(
            ["image_generation_id"],
            ["image_generations.id"],
            name=op.f(
                "fk_image_generation_outputs_image_generation_id_image_generations"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_image_generation_outputs")),
        sa.UniqueConstraint(
            "image_generation_id",
            "index",
            name="uq_image_generation_outputs_gen_index",
        ),
    )
    op.create_index(
        "ix_image_generation_outputs_r2_key",
        "image_generation_outputs",
        ["r2_key"],
        unique=False,
        postgresql_where=sa.text("r2_key IS NOT NULL"),
    )

    # Close the deferred FK provisioned in 0006 (same pattern as 0008 for
    # media_assets.ai_decision_id). Clear dangling refs first so a downgrade→
    # upgrade round-trip against a DB that kept generated assets still works —
    # without the FK, PostgreSQL cannot cascade-null on table drop.
    op.execute(
        sa.text(
            "UPDATE media_assets SET image_generation_output_id = NULL "
            "WHERE image_generation_output_id IS NOT NULL"
        )
    )
    op.create_foreign_key(
        op.f("fk_media_assets_image_generation_output_id_image_generation_outputs"),
        "media_assets",
        "image_generation_outputs",
        ["image_generation_output_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_media_assets_image_generation_output_id_image_generation_outputs"),
        "media_assets",
        type_="foreignkey",
    )
    # Dropping image_generation_outputs would otherwise leave orphan UUIDs in
    # media_assets that block re-creating the FK on the next upgrade.
    op.execute(
        sa.text(
            "UPDATE media_assets SET image_generation_output_id = NULL "
            "WHERE image_generation_output_id IS NOT NULL"
        )
    )
    op.drop_index(
        "ix_image_generation_outputs_r2_key",
        table_name="image_generation_outputs",
    )
    op.drop_table("image_generation_outputs")
    op.drop_index(
        "ix_image_generations_brand_created",
        table_name="image_generations",
    )
    op.drop_table("image_generations")
