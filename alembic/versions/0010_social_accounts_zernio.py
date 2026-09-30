"""Social accounts and Zernio (Phase 9)

Creates zernio_credentials, zernio_profiles, social_accounts,
social_account_connect_attempts, and webhook_events (architecture/02 §5 / §9).
Seeds three global credential rows (t1/t2/t3). Extends notifications.type CHECK
with channel_disconnected (architecture/02 §10).

Revision ID: 0010_social_accounts_zernio
Revises: 0009_brand_research
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010_social_accounts_zernio"
down_revision: str | None = "0009_brand_research"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_OLD_NOTIFICATION_TYPES = (
    "type IN ("
    "'approval_requested', 'post_approved', 'post_rejected', 'post_published', "
    "'post_failed', 'changes_requested', 'channel_expiring', 'weekly_insight', "
    "'plan_generated', 'mention')"
)

_NEW_NOTIFICATION_TYPES = (
    "type IN ("
    "'approval_requested', 'post_approved', 'post_rejected', 'post_published', "
    "'post_failed', 'changes_requested', 'channel_expiring', 'channel_disconnected', "
    "'weekly_insight', 'plan_generated', 'mention')"
)


def upgrade() -> None:
    # ------------------------------------------------------------------
    # zernio_credentials — global, operator-managed (architecture/02 §5 +
    # architecture/10 §2 health tracking)
    # ------------------------------------------------------------------
    op.create_table(
        "zernio_credentials",
        sa.Column("alias", sa.Text(), nullable=False),
        sa.Column("secret_ref", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
        sa.Column("max_profiles", sa.Integer(), nullable=False),
        sa.Column("plan_tier", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "connected_accounts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("last_401_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_402_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_429_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_5xx_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rate_limited_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rate_limit_remaining", sa.Integer(), nullable=True),
        sa.Column("rate_limit_reset_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_health_check_at", sa.DateTime(timezone=True), nullable=True),
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
            "status IN ('active', 'draining', 'disabled')",
            name=op.f("ck_zernio_credentials_status_valid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_zernio_credentials")),
        sa.UniqueConstraint("alias", name=op.f("uq_zernio_credentials_alias")),
    )

    op.execute(
        sa.text(
            """
            INSERT INTO zernio_credentials (alias, secret_ref, status, max_profiles)
            VALUES
              ('t1', 'ZERNIO_API_KEY__t1', 'active', 100),
              ('t2', 'ZERNIO_API_KEY__t2', 'active', 100),
              ('t3', 'ZERNIO_API_KEY__t3', 'active', 100)
            """
        )
    )

    # ------------------------------------------------------------------
    # zernio_profiles — BrandScoped + soft delete
    # ------------------------------------------------------------------
    op.create_table(
        "zernio_profiles",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("credential_id", sa.Uuid(), nullable=False),
        sa.Column("zernio_profile_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="active", nullable=False),
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
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Uuid(), nullable=True),
        sa.CheckConstraint(
            "status IN ('active', 'deleted')",
            name=op.f("ck_zernio_profiles_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_zernio_profiles_brand_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["credential_id"],
            ["zernio_credentials.id"],
            name=op.f("fk_zernio_profiles_credential_id_zernio_credentials"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["deleted_by"],
            ["users.id"],
            name=op.f("fk_zernio_profiles_deleted_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_zernio_profiles")),
        sa.UniqueConstraint(
            "credential_id",
            "zernio_profile_id",
            name="uq_zernio_profiles_credential_zernio_id",
        ),
        sa.UniqueConstraint(
            "id",
            "credential_id",
            name="uq_zernio_profiles_id_credential_id",
        ),
    )
    op.create_index(
        "ux_zprofile_brand_live",
        "zernio_profiles",
        ["brand_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # ------------------------------------------------------------------
    # social_accounts — BrandScoped, no soft delete
    # ------------------------------------------------------------------
    op.create_table(
        "social_accounts",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.Text(), nullable=False),
        sa.Column("zernio_profile_id", sa.Uuid(), nullable=True),
        sa.Column("credential_id", sa.Uuid(), nullable=True),
        sa.Column("zernio_account_id", sa.Text(), nullable=True),
        sa.Column("external_account_id", sa.Text(), nullable=True),
        sa.Column("handle", sa.Text(), server_default="", nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("avatar_url", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            server_default="disconnected",
            nullable=False,
        ),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_token_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connected_by", sa.Uuid(), nullable=True),
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
            "platform IN ('instagram', 'facebook', 'tiktok', 'snapchat', 'whatsapp')",
            name=op.f("ck_social_accounts_platform_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('connected', 'expiring', 'expired', 'disconnected')",
            name=op.f("ck_social_accounts_status_valid"),
        ),
        sa.CheckConstraint(
            "(zernio_profile_id IS NULL) = (credential_id IS NULL)",
            name=op.f("ck_social_accounts_pin_nullability_valid"),
        ),
        sa.CheckConstraint(
            "status = 'disconnected' OR "
            "(zernio_account_id IS NOT NULL AND zernio_profile_id IS NOT NULL)",
            name=op.f("ck_social_accounts_connected_pin_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_social_accounts_brand_org",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["zernio_profile_id", "credential_id"],
            ["zernio_profiles.id", "zernio_profiles.credential_id"],
            name="fk_social_accounts_profile_credential",
        ),
        sa.ForeignKeyConstraint(
            ["connected_by"],
            ["users.id"],
            name=op.f("fk_social_accounts_connected_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_social_accounts")),
    )
    op.create_index(
        "ux_social_brand_platform",
        "social_accounts",
        ["brand_id", "platform"],
        unique=True,
    )
    op.create_index(
        "ux_social_zernio_account",
        "social_accounts",
        ["zernio_account_id"],
        unique=True,
        postgresql_where=sa.text("zernio_account_id IS NOT NULL"),
    )
    op.create_index(
        "ix_social_token_expiry",
        "social_accounts",
        ["token_expires_at"],
        unique=False,
        postgresql_where=sa.text("status IN ('connected', 'expiring')"),
    )

    # ------------------------------------------------------------------
    # social_account_connect_attempts — append-only (id + created_at only)
    # ------------------------------------------------------------------
    op.create_table(
        "social_account_connect_attempts",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=False),
        sa.Column("social_account_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("oauth_state_hash", sa.Text(), nullable=True),
        sa.Column("zernio_profile_id", sa.Uuid(), nullable=True),
        sa.Column(
            "status",
            sa.Text(),
            server_default="started",
            nullable=False,
        ),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "provider_response",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "kind IN ('connect', 'reconnect', 'disconnect')",
            name=op.f("ck_social_account_connect_attempts_kind_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('started', 'succeeded', 'failed', 'abandoned')",
            name=op.f("ck_social_account_connect_attempts_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["social_account_id"],
            ["social_accounts.id"],
            name=op.f(
                "fk_social_account_connect_attempts_social_account_id_social_accounts"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_social_account_connect_attempts_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["zernio_profile_id"],
            ["zernio_profiles.id"],
            name=op.f(
                "fk_social_account_connect_attempts_zernio_profile_id_zernio_profiles"
            ),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_social_account_connect_attempts")),
    )
    op.create_index(
        "ix_connect_attempts",
        "social_account_connect_attempts",
        ["social_account_id", sa.literal_column("created_at DESC")],
        unique=False,
    )

    # ------------------------------------------------------------------
    # webhook_events — bigint identity PK (architecture/02 §9)
    # ------------------------------------------------------------------
    op.create_table(
        "webhook_events",
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("external_event_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "provider IN ('zernio', 'resend', 'fal')",
            name=op.f("ck_webhook_events_provider_valid"),
        ),
    )
    op.create_index(
        "ux_webhook_dedupe",
        "webhook_events",
        ["provider", "external_event_id"],
        unique=True,
    )
    op.create_index(
        "ix_webhook_unprocessed",
        "webhook_events",
        ["received_at"],
        unique=False,
        postgresql_where=sa.text("processed_at IS NULL"),
    )
    op.create_index(
        "ix_webhook_received_brin",
        "webhook_events",
        ["received_at"],
        unique=False,
        postgresql_using="brin",
    )

    # ------------------------------------------------------------------
    # notifications.type — add channel_disconnected (architecture/02 §10)
    # ------------------------------------------------------------------
    op.drop_constraint(
        op.f("ck_notifications_type_valid"), "notifications", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_notifications_type_valid"),
        "notifications",
        _NEW_NOTIFICATION_TYPES,
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("ck_notifications_type_valid"), "notifications", type_="check"
    )
    op.create_check_constraint(
        op.f("ck_notifications_type_valid"),
        "notifications",
        _OLD_NOTIFICATION_TYPES,
    )

    op.drop_index(
        "ix_webhook_received_brin",
        table_name="webhook_events",
        postgresql_using="brin",
    )
    op.drop_index("ix_webhook_unprocessed", table_name="webhook_events")
    op.drop_index("ux_webhook_dedupe", table_name="webhook_events")
    op.drop_table("webhook_events")

    op.drop_index("ix_connect_attempts", table_name="social_account_connect_attempts")
    op.drop_table("social_account_connect_attempts")

    op.drop_index("ix_social_token_expiry", table_name="social_accounts")
    op.drop_index("ux_social_zernio_account", table_name="social_accounts")
    op.drop_index("ux_social_brand_platform", table_name="social_accounts")
    op.drop_table("social_accounts")

    op.drop_index("ux_zprofile_brand_live", table_name="zernio_profiles")
    op.drop_table("zernio_profiles")

    op.drop_table("zernio_credentials")
