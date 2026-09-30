"""Media and storage (Phase 5)

Creates upload_intents, media_assets, and media_variants
(architecture/02 §4). All three tables are new and empty at deploy time.

Revision ID: 0006_media_storage
Revises: 0005_seed_cultural_events
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_media_storage"
down_revision: str | None = "0005_seed_cultural_events"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "upload_intents",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("r2_bucket", sa.Text(), nullable=False),
        sa.Column("r2_key", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("declared_size", sa.BigInteger(), nullable=False),
        sa.Column("presign_headers", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "declared_size > 0", name=op.f("ck_upload_intents_declared_size_positive")
        ),
        sa.ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name=op.f("fk_upload_intents_brand_org"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_upload_intents_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_upload_intents_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_upload_intents")),
        sa.UniqueConstraint("r2_key", name=op.f("uq_upload_intents_r2_key")),
    )
    op.create_index(
        "ix_upload_intents_expiry",
        "upload_intents",
        ["expires_at"],
        unique=False,
        postgresql_where=sa.text("completed_at IS NULL"),
    )

    op.create_table(
        "media_assets",
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="ready", nullable=False),
        sa.Column("r2_bucket", sa.Text(), nullable=False),
        sa.Column("r2_key", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("checksum_sha256", sa.Text(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Numeric(8, 2), nullable=True),
        sa.Column("poster_r2_key", sa.Text(), nullable=True),
        sa.Column("alt_ar", sa.Text(), server_default="", nullable=False),
        sa.Column("alt_en", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("focal_x", sa.Numeric(4, 3), nullable=True),
        sa.Column("focal_y", sa.Numeric(4, 3), nullable=True),
        sa.Column("attribution", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "template_params", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column("generation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("ai_decision_id", sa.Uuid(), nullable=True),
        sa.Column("image_generation_output_id", sa.Uuid(), nullable=True),
        sa.Column("uploaded_by", sa.Uuid(), nullable=True),
        sa.Column("original_filename", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "kind IN ('image', 'video', 'template')",
            name=op.f("ck_media_assets_kind_valid"),
        ),
        sa.CheckConstraint(
            "source IN ('upload', 'stock', 'generated', 'template')",
            name=op.f("ck_media_assets_source_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('ready', 'processing', 'failed')",
            name=op.f("ck_media_assets_status_valid"),
        ),
        sa.CheckConstraint("width > 0", name=op.f("ck_media_assets_width_positive")),
        sa.CheckConstraint("height > 0", name=op.f("ck_media_assets_height_positive")),
        sa.CheckConstraint(
            "(focal_x IS NULL) = (focal_y IS NULL)",
            name=op.f("ck_media_assets_focal_both_or_neither"),
        ),
        sa.CheckConstraint(
            "focal_x IS NULL OR (focal_x BETWEEN 0 AND 1 AND focal_y BETWEEN 0 AND 1)",
            name=op.f("ck_media_assets_focal_in_unit_square"),
        ),
        sa.CheckConstraint(
            "source <> 'stock' OR attribution IS NOT NULL",
            name=op.f("ck_media_assets_stock_requires_attribution"),
        ),
        sa.CheckConstraint(
            "(kind = 'template') = (template_params IS NOT NULL)",
            name=op.f("ck_media_assets_template_params_iff_template"),
        ),
        sa.ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name=op.f("fk_media_assets_brand_org"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by"],
            ["users.id"],
            name=op.f("fk_media_assets_deleted_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by"],
            ["users.id"],
            name=op.f("fk_media_assets_uploaded_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_assets")),
        sa.UniqueConstraint("id", "brand_id", name="uq_media_assets_id_brand_id"),
        sa.UniqueConstraint("r2_key", name=op.f("uq_media_assets_r2_key")),
    )
    op.create_index(
        "ix_media_brand_created",
        "media_assets",
        ["brand_id", sa.literal_column("created_at DESC"), sa.literal_column("id DESC")],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_media_tags",
        "media_assets",
        ["tags"],
        unique=False,
        postgresql_using="gin",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "media_variants",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("media_asset_id", sa.Uuid(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("aspect", sa.Text(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=True),
        sa.Column("r2_key", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "purpose IN ('thumb', 'crop', 'render', 'poster')",
            name=op.f("ck_media_variants_purpose_valid"),
        ),
        sa.CheckConstraint(
            "aspect IN ('square', 'portrait', 'vertical', 'landscape', 'original')",
            name=op.f("ck_media_variants_aspect_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["media_asset_id"],
            ["media_assets.id"],
            name=op.f("fk_media_variants_media_asset_id_media_assets"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_media_variants")),
        sa.UniqueConstraint("r2_key", name=op.f("uq_media_variants_r2_key")),
    )
    op.create_index(
        "ux_media_variants",
        "media_variants",
        [
            "media_asset_id",
            "purpose",
            "aspect",
            sa.literal_column("coalesce(platform, '')"),
        ],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_media_variants", table_name="media_variants")
    op.drop_table("media_variants")
    op.drop_index("ix_media_tags", table_name="media_assets")
    op.drop_index("ix_media_brand_created", table_name="media_assets")
    op.drop_table("media_assets")
    op.drop_index("ix_upload_intents_expiry", table_name="upload_intents")
    op.drop_table("upload_intents")
