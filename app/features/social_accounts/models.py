"""Social accounts and Zernio — architecture/02 §5 / §9 / architecture/10 §2."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    BrandScopedMixin,
    CreatedAtMixin,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class ZernioCredential(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Global Zernio API-key pool row — not tenant-scoped (architecture/02 §5)."""

    __tablename__ = "zernio_credentials"

    alias: Mapped[str] = mapped_column(unique=True)
    secret_ref: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="active")
    max_profiles: Mapped[int]
    max_accounts: Mapped[int] = mapped_column(Integer, server_default=text("2"))
    plan_tier: Mapped[str | None] = mapped_column(default=None)
    notes: Mapped[str | None] = mapped_column(default=None)
    connected_accounts: Mapped[int] = mapped_column(
        Integer, server_default=text("0")
    )
    last_401_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_402_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_429_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_5xx_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    rate_limited_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    rate_limit_remaining: Mapped[int | None] = mapped_column(Integer, default=None)
    rate_limit_reset_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_health_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'draining', 'disabled')",
            name="status_valid",
        ),
    )


class ZernioProfile(Base, UUIDPrimaryKeyMixin, TimestampMixin, BrandScopedMixin, SoftDeleteMixin):
    """One live Zernio profile pin per (brand, credential) — brands may spill across keys."""

    __tablename__ = "zernio_profiles"

    credential_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("zernio_credentials.id", ondelete="RESTRICT"), nullable=False
    )
    zernio_profile_id: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="active")

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'deleted')",
            name="status_valid",
        ),
        ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            ondelete="CASCADE",
            name="fk_zernio_profiles_brand_org",
        ),
        UniqueConstraint(
            "credential_id",
            "zernio_profile_id",
            name="uq_zernio_profiles_credential_zernio_id",
        ),
        UniqueConstraint(
            "id",
            "credential_id",
            name="uq_zernio_profiles_id_credential_id",
        ),
        Index(
            "ux_zprofile_brand_credential_live",
            "brand_id",
            "credential_id",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class SocialAccount(Base, UUIDPrimaryKeyMixin, TimestampMixin, BrandScopedMixin):
    """One row per (brand, platform) — placeholder until connected (architecture/02 §5)."""

    __tablename__ = "social_accounts"

    platform: Mapped[str]
    zernio_profile_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    credential_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    zernio_account_id: Mapped[str | None] = mapped_column(default=None)
    external_account_id: Mapped[str | None] = mapped_column(default=None)
    handle: Mapped[str] = mapped_column(server_default="")
    display_name: Mapped[str | None] = mapped_column(default=None)
    avatar_url: Mapped[str | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(server_default="disconnected")
    token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_token_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    scopes: Mapped[list[str] | None] = mapped_column(ARRAY(Text), default=None)
    last_error_code: Mapped[str | None] = mapped_column(default=None)
    last_error_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    disconnected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    connected_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    __table_args__ = (
        CheckConstraint(
            "platform IN ('instagram', 'facebook', 'tiktok', 'snapchat', 'whatsapp')",
            name="platform_valid",
        ),
        CheckConstraint(
            "status IN ('connected', 'expiring', 'expired', 'disconnected')",
            name="status_valid",
        ),
        CheckConstraint(
            "(zernio_profile_id IS NULL) = (credential_id IS NULL)",
            name="pin_nullability_valid",
        ),
        CheckConstraint(
            "status = 'disconnected' OR "
            "(zernio_account_id IS NOT NULL AND zernio_profile_id IS NOT NULL)",
            name="connected_pin_valid",
        ),
        ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            ondelete="CASCADE",
            name="fk_social_accounts_brand_org",
        ),
        ForeignKeyConstraint(
            ["zernio_profile_id", "credential_id"],
            ["zernio_profiles.id", "zernio_profiles.credential_id"],
            name="fk_social_accounts_profile_credential",
        ),
        Index(
            "ux_social_brand_platform",
            "brand_id",
            "platform",
            unique=True,
        ),
        Index(
            "ux_social_zernio_account",
            "zernio_account_id",
            unique=True,
            postgresql_where=text("zernio_account_id IS NOT NULL"),
        ),
        Index(
            "ix_social_token_expiry",
            "token_expires_at",
            postgresql_where=text("status IN ('connected', 'expiring')"),
        ),
    )


class SocialAccountConnectAttempt(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Append-only connect/reconnect/disconnect attempt log (architecture/02 §5)."""

    __tablename__ = "social_account_connect_attempts"

    organization_id: Mapped[uuid.UUID]
    brand_id: Mapped[uuid.UUID]
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("social_accounts.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str]
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    oauth_state_hash: Mapped[str | None] = mapped_column(default=None)
    zernio_profile_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("zernio_profiles.id"), default=None
    )
    status: Mapped[str] = mapped_column(server_default="started")
    error_code: Mapped[str | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(default=None)
    provider_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('connect', 'reconnect', 'disconnect')",
            name="kind_valid",
        ),
        CheckConstraint(
            "status IN ('started', 'succeeded', 'failed', 'abandoned')",
            name="status_valid",
        ),
        Index(
            "ix_connect_attempts",
            "social_account_id",
            text("created_at DESC"),
        ),
    )


class WebhookEvent(Base):
    """Inbound webhook ledger — bigint identity PK (architecture/02 §9)."""

    __tablename__ = "webhook_events"
    __table_args__ = (
        CheckConstraint(
            "provider IN ('zernio', 'resend', 'fal')",
            name="provider_valid",
        ),
        Index(
            "ux_webhook_dedupe",
            "provider",
            "external_event_id",
            unique=True,
        ),
        Index(
            "ix_webhook_unprocessed",
            "received_at",
            postgresql_where=text("processed_at IS NULL"),
        ),
        Index(
            "ix_webhook_received_brin",
            "received_at",
            postgresql_using="brin",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger(), Identity(always=True), primary_key=True
    )
    provider: Mapped[str]
    external_event_id: Mapped[str]
    event_type: Mapped[str]
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    signature_valid: Mapped[bool] = mapped_column(Boolean)
    received_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    processing_error: Mapped[str | None] = mapped_column(default=None)
