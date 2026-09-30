"""Publications ledger (Phase 10)

Creates publications with the corrected status CHECK (accepted, skipped) and
the trigger column (architecture/02 §6 as amended by Phase 10; architecture/10
§6). posts.zernio_post_id already exists from 0007 — not touched here.

Revision ID: 0012_publications
Revises: 0011_backfill_social_accts
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012_publications"
down_revision: str | None = "0011_backfill_social_accts"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "publications",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("post_id", sa.Uuid(), nullable=False),
        sa.Column("social_account_id", sa.Uuid(), nullable=False),
        sa.Column("zernio_profile_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("zernio_post_id", sa.Text(), nullable=True),
        sa.Column("external_post_id", sa.Text(), nullable=True),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_category", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=True),
        sa.Column(
            "request_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "response_payload",
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
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt_no >= 1",
            name=op.f("ck_publications_attempt_no_positive"),
        ),
        sa.CheckConstraint(
            "trigger IN ("
            "'scheduled', 'publish_now', 'retry', 'auto_retry', 'resume')",
            name=op.f("ck_publications_trigger_valid"),
        ),
        sa.CheckConstraint(
            "status IN ("
            "'pending', 'sent', 'accepted', 'published', "
            "'failed', 'skipped', 'cancelled')",
            name=op.f("ck_publications_status_valid"),
        ),
        sa.CheckConstraint(
            "error_category IS NULL OR error_category IN ("
            "'auth', 'rate_limit', 'validation', 'platform', "
            "'network', 'frozen', 'internal')",
            name=op.f("ck_publications_error_category_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_publications_brand_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["posts.id"],
            name=op.f("fk_publications_post_id_posts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["social_account_id"],
            ["social_accounts.id"],
            name=op.f("fk_publications_social_account_id_social_accounts"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["zernio_profile_id"],
            ["zernio_profiles.id"],
            name=op.f("fk_publications_zernio_profile_id_zernio_profiles"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["zernio_credentials.id"],
            name=op.f("fk_publications_credential_id_zernio_credentials"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publications")),
        sa.UniqueConstraint(
            "post_id",
            "attempt_no",
            name="uq_publications_post_id_attempt_no",
        ),
        sa.UniqueConstraint(
            "idempotency_key",
            name=op.f("uq_publications_idempotency_key"),
        ),
    )
    # Hand-named partial unique indexes (architecture/03 naming exception).
    op.create_index(
        "ux_pub_inflight",
        "publications",
        ["post_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending', 'sent', 'accepted')"
        ),
    )
    op.create_index(
        "ux_pub_published",
        "publications",
        ["post_id"],
        unique=True,
        postgresql_where=sa.text("status = 'published'"),
    )
    op.create_index(
        "ux_pub_zernio_post",
        "publications",
        ["zernio_post_id"],
        unique=True,
        postgresql_where=sa.text("zernio_post_id IS NOT NULL"),
    )
    op.create_index(
        "ix_publications_org_created",
        "publications",
        ["organization_id", sa.literal_column("created_at DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_publications_org_created", table_name="publications")
    op.drop_index("ux_pub_zernio_post", table_name="publications")
    op.drop_index("ux_pub_published", table_name="publications")
    op.drop_index("ux_pub_inflight", table_name="publications")
    op.drop_table("publications")
