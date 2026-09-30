from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, LargeBinary, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class User(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """architecture/02 §1."""

    __tablename__ = "users"

    email: Mapped[str]
    password_hash: Mapped[str | None] = mapped_column(default=None)
    name: Mapped[str]
    name_ar: Mapped[str] = mapped_column(server_default="")
    avatar_url: Mapped[str | None] = mapped_column(default=None)
    locale: Mapped[str] = mapped_column(server_default="ar")
    email_verified_at: Mapped[datetime | None] = mapped_column(default=None)
    mfa_enabled: Mapped[bool] = mapped_column(server_default=text("false"))
    last_login_at: Mapped[datetime | None] = mapped_column(default=None)
    deactivated_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        Index("ux_users_email_lower", text("lower(email)"), unique=True),
        CheckConstraint("locale IN ('ar', 'en')", name="locale_valid"),
    )


class Session(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """architecture/02 §1. No `updated_at` — a session is only ever revoked
    (`revoked_at`), never otherwise modified after creation.
    """

    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    token_hash: Mapped[str] = mapped_column(unique=True)
    expires_at: Mapped[datetime]
    remember_me: Mapped[bool] = mapped_column(server_default=text("false"))
    mfa_verified_at: Mapped[datetime | None] = mapped_column(default=None)
    ip: Mapped[str | None] = mapped_column(INET, default=None)
    user_agent: Mapped[str | None] = mapped_column(default=None)
    last_seen_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        Index("ix_sessions_user", "user_id", postgresql_where=text("revoked_at IS NULL")),
        Index("ix_sessions_expiry", "expires_at"),
    )


class PasswordResetToken(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """architecture/02 §1. 1-hour expiry, single use (`used_at`)."""

    __tablename__ = "password_reset_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(unique=True)
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None] = mapped_column(default=None)
    requested_ip: Mapped[str | None] = mapped_column(INET, default=None)


class Invitation(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """architecture/02 §1. The contract's `TeamMember.id` resolves to `users.id`
    for an active member, `invitations.id` for a pending one — `DELETE
    /team/:memberId` tries both tables in turn (features/team).
    """

    __tablename__ = "invitations"

    email: Mapped[str]
    role: Mapped[str]
    token_hash: Mapped[str] = mapped_column(unique=True)
    invited_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[datetime]
    resent_at: Mapped[datetime | None] = mapped_column(default=None)
    accepted_at: Mapped[datetime | None] = mapped_column(default=None)
    accepted_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint(
            "role IN ('owner', 'admin', 'editor', 'approver', 'viewer')", name="role_valid"
        ),
        Index(
            "ux_invite_pending_email",
            "organization_id",
            text("lower(email)"),
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
    )


class MfaCredential(Base, TimestampMixin):
    """architecture/02 §1. PK is `user_id`, not a uuidv7 id — at most one
    credential per user, so the natural key is the FK itself.
    """

    __tablename__ = "mfa_credentials"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    totp_secret_enc: Mapped[bytes] = mapped_column(LargeBinary)
    key_id: Mapped[str]
    verified_at: Mapped[datetime | None] = mapped_column(default=None)
    last_used_step: Mapped[int | None] = mapped_column(default=None)


class MfaRecoveryCode(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """architecture/02 §1."""

    __tablename__ = "mfa_recovery_codes"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    code_hash: Mapped[str]
    used_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (UniqueConstraint("user_id", "code_hash"),)


class MfaChallenge(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """architecture/02 §1. Backs the login response's `challengeToken` for an
    MFA-enabled account — 5-minute expiry.
    """

    __tablename__ = "mfa_challenges"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(unique=True)
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None] = mapped_column(default=None)
    remember_me: Mapped[bool]
    ip: Mapped[str | None] = mapped_column(INET, default=None)


class OauthState(Base, CreatedAtMixin):
    """architecture/02 §1. `brand_id` → brands(id) was added in Phase 4 once
    the target table existed. Nothing enqueues a row until Phase 9's Zernio
    connect flow; this table exists now only to fix its platform CHECK
    constraint once, early.
    """

    __tablename__ = "oauth_states"

    state_hash: Mapped[str] = mapped_column(primary_key=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE")
    )
    brand_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("brands.id"))
    platform: Mapped[str]
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint(
            "platform IN ('instagram', 'facebook', 'tiktok', 'snapchat', 'whatsapp')",
            name="platform_valid",
        ),
        Index("ix_oauth_states_expiry", "expires_at"),
    )


class UserIdentity(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Linked IdP identity (Google today). One row per provider subject."""

    __tablename__ = "user_identities"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[str]
    provider_subject: Mapped[str]
    email: Mapped[str | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint("provider IN ('google')", name="provider_valid"),
        UniqueConstraint(
            "provider",
            "provider_subject",
            name="uq_user_identities_provider_subject",
        ),
        Index("ix_user_identities_user", "user_id"),
    )


class AuthOauthState(Base, CreatedAtMixin):
    """CSRF/OIDC state for Google login — not channel oauth_states."""

    __tablename__ = "auth_oauth_states"

    state_hash: Mapped[str] = mapped_column(primary_key=True)
    nonce: Mapped[str]
    remember_me: Mapped[bool] = mapped_column(server_default=text("false"))
    locale: Mapped[str] = mapped_column(server_default="en")
    expires_at: Mapped[datetime]
    consumed_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint("locale IN ('ar', 'en')", name="locale_valid"),
        Index("ix_auth_oauth_states_expiry", "expires_at"),
    )
