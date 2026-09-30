"""Posts workflow (Phase 6)

Creates posts, post_status_transitions, post_media, post_versions,
post_comments, review_links, and review_link_posts (architecture/02 §6–§7).
Seeds the 17 legal status transitions and installs the BEFORE UPDATE OF status
trigger. publications is intentionally deferred to Phase 10.

Revision ID: 0007_posts_workflow
Revises: 0006_media_storage
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_posts_workflow"
down_revision: str | None = "0006_media_storage"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_SCHEDULED_DATE_RIYADH = (
    "DATE '1970-01-01' + floor("
    "(extract(epoch from (scheduled_at - TIMESTAMPTZ '1970-01-01 00:00:00+00')) "
    "+ 10800) / 86400)::int"
)

_TRANSITIONS: list[tuple[str, str]] = [
    ("idea", "drafting"),
    ("drafting", "draft"),
    ("draft", "in_review"),
    ("in_review", "approved"),
    ("in_review", "changes_requested"),
    ("in_review", "draft"),
    ("changes_requested", "drafting"),
    ("changes_requested", "draft"),
    ("changes_requested", "in_review"),
    ("approved", "scheduled"),
    ("scheduled", "publishing"),
    ("scheduled", "draft"),
    ("publishing", "published"),
    ("publishing", "failed"),
    ("published", "draft"),
    ("failed", "scheduled"),
    ("failed", "draft"),
]


def upgrade() -> None:
    op.create_table(
        "post_status_transitions",
        sa.Column("from_status", sa.Text(), nullable=False),
        sa.Column("to_status", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint(
            "from_status", "to_status", name=op.f("pk_post_status_transitions")
        ),
    )

    # posts without approved_review_link_id FK yet (circular with review_links).
    op.create_table(
        "posts",
        sa.Column("group_id", sa.Uuid(), nullable=True),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="draft", nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("internal_note", sa.Text(), nullable=True),
        sa.Column("pillar_id", sa.Uuid(), nullable=True),
        sa.Column("cultural_event_id", sa.Uuid(), nullable=True),
        sa.Column("is_paid", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "scheduled_date_riyadh",
            sa.Date(),
            sa.Computed(_SCHEDULED_DATE_RIYADH, persisted=True),
            nullable=False,
        ),
        sa.Column("schedule_epoch", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("first_comment", sa.Text(), nullable=True),
        sa.Column(
            "variants",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "risk",
            postgresql.JSONB(astext_type=sa.Text()),
            # Space after colon so SQLAlchemy does not treat `:0` as a bind param.
            server_default=sa.text("""'{"score": 0, "reasons": []}'::jsonb"""),
            nullable=False,
        ),
        sa.Column(
            "risk_score",
            sa.Numeric(4, 3),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("change_request_reason", sa.Text(), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.Column("last_error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("zernio_post_id", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("approved_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("approved_via", sa.Text(), nullable=True),
        sa.Column("approved_review_link_id", sa.Uuid(), nullable=True),
        sa.Column("ai_decision_id", sa.Uuid(), nullable=True),
        sa.Column("brand_version", sa.Integer(), nullable=True),
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
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "platform IN ('instagram', 'facebook', 'tiktok', 'snapchat', 'whatsapp')",
            name=op.f("ck_posts_platform_valid"),
        ),
        sa.CheckConstraint(
            "status IN ("
            "'idea', 'drafting', 'draft', 'in_review', 'changes_requested', "
            "'approved', 'scheduled', 'publishing', 'published', 'failed')",
            name=op.f("ck_posts_status_valid"),
        ),
        sa.CheckConstraint(
            "approved_via IS NULL OR approved_via IN ('user', 'policy', 'review_link')",
            name=op.f("ck_posts_approved_via_valid"),
        ),
        sa.CheckConstraint(
            "jsonb_typeof(variants) = 'array' AND jsonb_array_length(variants) <= 2",
            name=op.f("ck_posts_variants_array_max_2"),
        ),
        sa.CheckConstraint(
            "risk_score BETWEEN 0 AND 1",
            name=op.f("ck_posts_risk_score_unit_interval"),
        ),
        sa.CheckConstraint(
            "status <> 'published' OR published_at IS NOT NULL",
            name=op.f("ck_posts_published_requires_published_at"),
        ),
        sa.CheckConstraint(
            "status <> 'in_review' OR submitted_at IS NOT NULL",
            name=op.f("ck_posts_in_review_requires_submitted_at"),
        ),
        sa.ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name=op.f("fk_posts_brand_org"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["pillar_id", "brand_id"],
            ["content_pillars.id", "content_pillars.brand_id"],
            name=op.f("fk_posts_pillar_brand"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["cultural_event_id"],
            ["cultural_events.id"],
            name=op.f("fk_posts_cultural_event_id_cultural_events"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_posts_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by_user_id"],
            ["users.id"],
            name=op.f("fk_posts_approved_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by"],
            ["users.id"],
            name=op.f("fk_posts_deleted_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_posts")),
        sa.UniqueConstraint("id", "brand_id", name="uq_posts_id_brand_id"),
    )
    op.create_index(
        "ix_posts_brand_sched",
        "posts",
        ["brand_id", "scheduled_at", "id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_posts_brand_status_sched",
        "posts",
        ["brand_id", "status", "scheduled_at", "id"],
        unique=False,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_posts_due",
        "posts",
        ["scheduled_at"],
        unique=False,
        postgresql_where=sa.text("status = 'scheduled' AND deleted_at IS NULL"),
    )
    op.create_index(
        "ix_posts_group",
        "posts",
        ["group_id"],
        unique=False,
        postgresql_where=sa.text("group_id IS NOT NULL"),
    )
    op.create_index(
        "ix_posts_publishing_stale",
        "posts",
        ["updated_at"],
        unique=False,
        postgresql_where=sa.text("status = 'publishing'"),
    )

    op.create_table(
        "post_media",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("post_id", sa.Uuid(), nullable=False),
        sa.Column("media_asset_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("alt_override", sa.Text(), nullable=True),
        sa.Column("crop", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["post_id", "brand_id"],
            ["posts.id", "posts.brand_id"],
            name=op.f("fk_post_media_post_brand"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["media_asset_id"],
            ["media_assets.id"],
            name=op.f("fk_post_media_media_asset_id_media_assets"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_post_media")),
        sa.UniqueConstraint("post_id", "position", name="uq_post_media_post_id_position"),
    )
    op.create_index("ix_post_media_asset", "post_media", ["media_asset_id"], unique=False)

    op.create_table(
        "post_versions",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("post_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("post_row_version", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), nullable=True),
        sa.Column("author_ref", sa.Text(), nullable=False),
        sa.Column("author_name", sa.Text(), nullable=False),
        sa.Column("brand_version", sa.Integer(), nullable=True),
        sa.Column("ai_decision_id", sa.Uuid(), nullable=True),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "reason IN ('created', 'edited', 'ai_generated', 'changes_applied', 'restored')",
            name=op.f("ck_post_versions_reason_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["posts.id"],
            name=op.f("fk_post_versions_post_id_posts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["users.id"],
            name=op.f("fk_post_versions_author_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_post_versions")),
        sa.UniqueConstraint("post_id", "version", name="uq_post_versions_post_id_version"),
    )

    op.create_table(
        "post_comments",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("post_id", sa.Uuid(), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), nullable=False),
        sa.Column("author_name", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("lang", sa.Text(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
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
            "lang IN ('ar', 'en')", name=op.f("ck_post_comments_lang_valid")
        ),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["posts.id"],
            name=op.f("fk_post_comments_post_id_posts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["users.id"],
            name=op.f("fk_post_comments_author_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by"],
            ["users.id"],
            name=op.f("fk_post_comments_resolved_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_post_comments")),
    )
    op.create_index(
        "ix_post_comments", "post_comments", ["post_id", "created_at"], unique=False
    )

    op.create_table(
        "review_links",
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("locale", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_name", sa.Text(), nullable=True),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("decided_ip", postgresql.INET(), nullable=True),
        sa.Column("view_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "locale IN ('ar', 'en')", name=op.f("ck_review_links_locale_valid")
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('approved', 'changes_requested')",
            name=op.f("ck_review_links_decision_valid"),
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decided_at IS NOT NULL",
            name=op.f("ck_review_links_decision_requires_decided_at"),
        ),
        sa.ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name=op.f("fk_review_links_brand_org"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_review_links_created_by_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_links")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_review_links_token_hash")),
    )

    op.create_foreign_key(
        op.f("fk_posts_approved_review_link_id_review_links"),
        "posts",
        "review_links",
        ["approved_review_link_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "review_link_posts",
        sa.Column("review_link_id", sa.Uuid(), nullable=False),
        sa.Column("post_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["review_link_id"],
            ["review_links.id"],
            name=op.f("fk_review_link_posts_review_link_id_review_links"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["posts.id"],
            name=op.f("fk_review_link_posts_post_id_posts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "review_link_id", "post_id", name=op.f("pk_review_link_posts")
        ),
    )
    op.create_index(
        "ix_review_link_posts", "review_link_posts", ["post_id"], unique=False
    )

    # Seed the 17 legal transitions (utils.ts TRANSITIONS — architecture/02's
    # "20" figure is a documentation discrepancy; seed matches the frontend SoT).
    op.bulk_insert(
        sa.table(
            "post_status_transitions",
            sa.column("from_status", sa.Text()),
            sa.column("to_status", sa.Text()),
        ),
        [{"from_status": f, "to_status": t} for f, t in _TRANSITIONS],
    )

    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION enforce_post_transition() RETURNS trigger AS $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM post_status_transitions
                    WHERE from_status = OLD.status AND to_status = NEW.status
                ) THEN
                    RAISE EXCEPTION 'INVALID_TRANSITION: % -> %', OLD.status, NEW.status
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
    )
    op.execute(
        sa.text(
            """
            CREATE TRIGGER trg_posts_enforce_transition
                BEFORE UPDATE OF status ON posts
                FOR EACH ROW
                EXECUTE FUNCTION enforce_post_transition();
            """
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP TRIGGER IF EXISTS trg_posts_enforce_transition ON posts"))
    op.execute(sa.text("DROP FUNCTION IF EXISTS enforce_post_transition()"))
    op.drop_index("ix_review_link_posts", table_name="review_link_posts")
    op.drop_table("review_link_posts")
    op.drop_constraint(
        op.f("fk_posts_approved_review_link_id_review_links"),
        "posts",
        type_="foreignkey",
    )
    op.drop_table("review_links")
    op.drop_index("ix_post_comments", table_name="post_comments")
    op.drop_table("post_comments")
    op.drop_table("post_versions")
    op.drop_index("ix_post_media_asset", table_name="post_media")
    op.drop_table("post_media")
    op.drop_index("ix_posts_publishing_stale", table_name="posts")
    op.drop_index("ix_posts_group", table_name="posts")
    op.drop_index("ix_posts_due", table_name="posts")
    op.drop_index("ix_posts_brand_status_sched", table_name="posts")
    op.drop_index("ix_posts_brand_sched", table_name="posts")
    op.drop_table("posts")
    op.drop_table("post_status_transitions")
