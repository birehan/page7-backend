"""Analytics and inbox tables (Phase 12)

Creates metric_snapshots, insight_reports, analytics_sync_state,
conversations, conversation_messages, and saved_replies
(architecture/02 §14/§16).

Revision ID: 0014_analytics_inbox
Revises: 0013_image_generation
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0014_analytics_inbox"
down_revision: str | None = "0013_image_generation"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_PLATFORM_CHK = "platform IN ('instagram', 'facebook', 'tiktok', 'snapchat', 'whatsapp')"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # metric_snapshots — bigint identity PK (architecture/02 §14)
    # ------------------------------------------------------------------
    op.create_table(
        "metric_snapshots",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("social_account_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("post_id", sa.Uuid(), nullable=True),
        sa.Column("metric_date", sa.Date(), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("impressions", sa.BigInteger(), nullable=True),
        sa.Column("reach", sa.BigInteger(), nullable=True),
        sa.Column("likes", sa.Integer(), nullable=True),
        sa.Column("comments", sa.Integer(), nullable=True),
        sa.Column("shares", sa.Integer(), nullable=True),
        sa.Column("saves", sa.Integer(), nullable=True),
        sa.Column("video_views", sa.BigInteger(), nullable=True),
        sa.Column("clicks", sa.Integer(), nullable=True),
        sa.Column("followers", sa.Integer(), nullable=True),
        sa.Column(
            "raw",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.CheckConstraint(
            _PLATFORM_CHK,
            name=op.f("ck_metric_snapshots_platform_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_metric_snapshots_brand_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["social_account_id"],
            ["social_accounts.id"],
            name=op.f("fk_metric_snapshots_social_account_id_social_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["posts.id"],
            name=op.f("fk_metric_snapshots_post_id_posts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_metric_snapshots")),
    )
    op.create_index(
        "ux_metric_post",
        "metric_snapshots",
        ["post_id", "metric_date"],
        unique=True,
        postgresql_where=sa.text("post_id IS NOT NULL"),
    )
    op.create_index(
        "ux_metric_account",
        "metric_snapshots",
        ["social_account_id", "metric_date"],
        unique=True,
        postgresql_where=sa.text("post_id IS NULL"),
    )
    op.create_index(
        "ix_metric_brand_date",
        "metric_snapshots",
        ["brand_id", "metric_date"],
        unique=False,
    )
    op.create_index(
        "ix_metric_captured_brin",
        "metric_snapshots",
        ["captured_at"],
        unique=False,
        postgresql_using="brin",
    )

    # ------------------------------------------------------------------
    # insight_reports
    # ------------------------------------------------------------------
    op.create_table(
        "insight_reports",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("week_of", sa.Date(), nullable=False),
        sa.Column("what_happened", sa.Text(), nullable=False),
        sa.Column("why", sa.Text(), nullable=False),
        sa.Column("what_to_change", sa.Text(), nullable=False),
        sa.Column(
            "next_actions",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("top_post_id", sa.Uuid(), nullable=True),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "series",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "pillar_breakdown",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "platform_breakdown",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("decision_id", sa.Uuid(), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_insight_reports_brand_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["top_post_id"],
            ["posts.id"],
            name=op.f("fk_insight_reports_top_post_id_posts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["ai_decisions.id"],
            name=op.f("fk_insight_reports_decision_id_ai_decisions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_insight_reports")),
        sa.UniqueConstraint(
            "brand_id",
            "week_of",
            name="uq_insight_reports_brand_id_week_of",
        ),
    )

    # ------------------------------------------------------------------
    # analytics_sync_state — credential-scoped, no org/brand
    # ------------------------------------------------------------------
    op.create_table(
        "analytics_sync_state",
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_cursor", sa.Text(), nullable=True),
        sa.Column("bootstrapped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_sync_status",
            sa.Text(),
            server_default="ok",
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "last_sync_status IN ('ok', 'error', 'gated')",
            name=op.f("ck_analytics_sync_state_last_sync_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["zernio_credentials.id"],
            name=op.f("fk_analytics_sync_state_credential_id_zernio_credentials"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("credential_id", name=op.f("pk_analytics_sync_state")),
    )

    # ------------------------------------------------------------------
    # conversations
    # ------------------------------------------------------------------
    op.create_table(
        "conversations",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("social_account_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("external_thread_id", sa.Text(), nullable=False),
        sa.Column("participant_name", sa.Text(), nullable=False),
        sa.Column("participant_handle", sa.Text(), nullable=True),
        sa.Column("participant_avatar_url", sa.Text(), nullable=True),
        sa.Column("participant_external_id", sa.Text(), nullable=True),
        sa.Column("post_id", sa.Uuid(), nullable=True),
        sa.Column("assignee_user_id", sa.Uuid(), nullable=True),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "sentiment",
            sa.Text(),
            server_default="neutral",
            nullable=False,
        ),
        sa.Column("sentiment_decision_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            server_default="open",
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "escalated",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "suggested_reply_ar",
            sa.Text(),
            server_default="",
            nullable=False,
        ),
        sa.Column(
            "suggested_reply_en",
            sa.Text(),
            server_default="",
            nullable=False,
        ),
        sa.Column("suggestion_decision_id", sa.Uuid(), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "message_count",
            sa.Integer(),
            server_default=sa.text("0"),
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
            _PLATFORM_CHK,
            name=op.f("ck_conversations_platform_valid"),
        ),
        sa.CheckConstraint(
            "kind IN ('dm', 'comment')",
            name=op.f("ck_conversations_kind_valid"),
        ),
        sa.CheckConstraint(
            "sentiment IN ('positive', 'neutral', 'negative')",
            name=op.f("ck_conversations_sentiment_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name=op.f("ck_conversations_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_conversations_brand_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["social_account_id"],
            ["social_accounts.id"],
            name=op.f("fk_conversations_social_account_id_social_accounts"),
        ),
        sa.ForeignKeyConstraint(
            ["post_id"],
            ["posts.id"],
            name=op.f("fk_conversations_post_id_posts"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["assignee_user_id"],
            ["users.id"],
            name=op.f("fk_conversations_assignee_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["sentiment_decision_id"],
            ["ai_decisions.id"],
            name=op.f("fk_conversations_sentiment_decision_id_ai_decisions"),
        ),
        sa.ForeignKeyConstraint(
            ["suggestion_decision_id"],
            ["ai_decisions.id"],
            name=op.f("fk_conversations_suggestion_decision_id_ai_decisions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        "ux_conv_external",
        "conversations",
        ["social_account_id", "external_thread_id"],
        unique=True,
    )
    op.create_index(
        "ix_conv_org_last",
        "conversations",
        [
            "organization_id",
            sa.literal_column("last_message_at DESC"),
            sa.literal_column("id DESC"),
        ],
        unique=False,
    )
    op.create_index(
        "ix_conv_open",
        "conversations",
        ["organization_id", sa.literal_column("last_message_at DESC")],
        unique=False,
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "ix_conv_tags",
        "conversations",
        ["tags"],
        unique=False,
        postgresql_using="gin",
    )
    op.create_index(
        "ix_conv_assignee",
        "conversations",
        ["assignee_user_id"],
        unique=False,
        postgresql_where=sa.text("status = 'open'"),
    )

    # ------------------------------------------------------------------
    # conversation_messages — ascending thread order (no DESC)
    # ------------------------------------------------------------------
    op.create_table(
        "conversation_messages",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("lang", sa.Text(), nullable=False),
        sa.Column("author_user_id", sa.Uuid(), nullable=True),
        sa.Column("author_name", sa.Text(), nullable=False),
        sa.Column("external_message_id", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("delivery_status", sa.Text(), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')",
            name=op.f("ck_conversation_messages_direction_valid"),
        ),
        sa.CheckConstraint(
            "lang IN ('ar', 'en')",
            name=op.f("ck_conversation_messages_lang_valid"),
        ),
        sa.CheckConstraint(
            "delivery_status IN ('received', 'pending', 'sent', 'failed')",
            name=op.f("ck_conversation_messages_delivery_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_conversation_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_user_id"],
            ["users.id"],
            name=op.f("fk_conversation_messages_author_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversation_messages")),
    )
    op.create_index(
        "ix_conv_messages",
        "conversation_messages",
        ["conversation_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ux_conv_message_external",
        "conversation_messages",
        ["conversation_id", "external_message_id"],
        unique=True,
        postgresql_where=sa.text("external_message_id IS NOT NULL"),
    )
    op.create_index(
        "ux_conv_message_idempotency",
        "conversation_messages",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    # ------------------------------------------------------------------
    # saved_replies — tenant-scoped, soft-delete
    # ------------------------------------------------------------------
    op.create_table(
        "saved_replies",
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body_ar", sa.Text(), nullable=False),
        sa.Column("body_en", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "usage_count",
            sa.Integer(),
            server_default=sa.text("0"),
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
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name=op.f("fk_saved_replies_organization_id_organizations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_saved_replies_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by"],
            ["users.id"],
            name=op.f("fk_saved_replies_deleted_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_saved_replies")),
    )
    op.create_index(
        "ux_saved_reply_title",
        "saved_replies",
        ["organization_id", sa.literal_column("lower(title)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ux_saved_reply_title",
        table_name="saved_replies",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_table("saved_replies")

    op.drop_index(
        "ux_conv_message_idempotency",
        table_name="conversation_messages",
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.drop_index(
        "ux_conv_message_external",
        table_name="conversation_messages",
        postgresql_where=sa.text("external_message_id IS NOT NULL"),
    )
    op.drop_index("ix_conv_messages", table_name="conversation_messages")
    op.drop_table("conversation_messages")

    op.drop_index(
        "ix_conv_assignee",
        table_name="conversations",
        postgresql_where=sa.text("status = 'open'"),
    )
    op.drop_index("ix_conv_tags", table_name="conversations", postgresql_using="gin")
    op.drop_index(
        "ix_conv_open",
        table_name="conversations",
        postgresql_where=sa.text("status = 'open'"),
    )
    op.drop_index("ix_conv_org_last", table_name="conversations")
    op.drop_index("ux_conv_external", table_name="conversations")
    op.drop_table("conversations")

    op.drop_table("analytics_sync_state")
    op.drop_table("insight_reports")

    op.drop_index(
        "ix_metric_captured_brin",
        table_name="metric_snapshots",
        postgresql_using="brin",
    )
    op.drop_index("ix_metric_brand_date", table_name="metric_snapshots")
    op.drop_index(
        "ux_metric_account",
        table_name="metric_snapshots",
        postgresql_where=sa.text("post_id IS NULL"),
    )
    op.drop_index(
        "ux_metric_post",
        table_name="metric_snapshots",
        postgresql_where=sa.text("post_id IS NOT NULL"),
    )
    op.drop_table("metric_snapshots")
