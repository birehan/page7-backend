"""Publications ledger — architecture/02 §6 (Phase 10 corrections) + architecture/10 §6."""

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
    Index,
    Integer,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BrandScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin

_TRIGGER_VALUES = (
    "scheduled",
    "publish_now",
    "retry",
    "auto_retry",
    "resume",
)
_STATUS_VALUES = (
    "pending",
    "sent",
    "accepted",
    "published",
    "failed",
    "skipped",
    "cancelled",
)
_ERROR_CATEGORY_VALUES = (
    "auth",
    "rate_limit",
    "validation",
    "platform",
    "network",
    "frozen",
    "internal",
)


class Publication(Base, UUIDPrimaryKeyMixin, TimestampMixin, BrandScopedMixin):
    """One row per Zernio submission attempt — append-only except result fields."""

    __tablename__ = "publications"

    post_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("posts.id", ondelete="RESTRICT"), nullable=False
    )
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("social_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    zernio_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("zernio_profiles.id", ondelete="RESTRICT"), nullable=False
    )
    credential_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("zernio_credentials.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    # ``trigger`` is a SQL reserved word; SQLAlchemy quotes it automatically.
    trigger: Mapped[str]
    idempotency_key: Mapped[str] = mapped_column(unique=True)
    job_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    status: Mapped[str] = mapped_column(server_default="pending")
    zernio_post_id: Mapped[str | None] = mapped_column(default=None)
    external_post_id: Mapped[str | None] = mapped_column(default=None)
    external_url: Mapped[str | None] = mapped_column(default=None)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    error_code: Mapped[str | None] = mapped_column(default=None)
    error_category: Mapped[str | None] = mapped_column(default=None)
    error_message: Mapped[str | None] = mapped_column(default=None)
    retryable: Mapped[bool | None] = mapped_column(Boolean, default=None)
    request_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, default=None
    )
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, default=None
    )

    __table_args__ = (
        CheckConstraint("attempt_no >= 1", name="attempt_no_positive"),
        CheckConstraint(
            "trigger IN ("
            + ", ".join(f"'{v}'" for v in _TRIGGER_VALUES)
            + ")",
            name="trigger_valid",
        ),
        CheckConstraint(
            "status IN ("
            + ", ".join(f"'{v}'" for v in _STATUS_VALUES)
            + ")",
            name="status_valid",
        ),
        CheckConstraint(
            "error_category IS NULL OR error_category IN ("
            + ", ".join(f"'{v}'" for v in _ERROR_CATEGORY_VALUES)
            + ")",
            name="error_category_valid",
        ),
        ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            ondelete="CASCADE",
            name="fk_publications_brand_org",
        ),
        UniqueConstraint(
            "post_id",
            "attempt_no",
            name="uq_publications_post_id_attempt_no",
        ),
        Index(
            "ux_pub_inflight",
            "post_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending', 'sent', 'accepted')"
            ),
        ),
        Index(
            "ux_pub_published",
            "post_id",
            unique=True,
            postgresql_where=text("status = 'published'"),
        ),
        Index(
            "ux_pub_zernio_post",
            "zernio_post_id",
            unique=True,
            postgresql_where=text("zernio_post_id IS NOT NULL"),
        ),
        Index(
            "ix_publications_org_created",
            "organization_id",
            text("created_at DESC"),
        ),
    )
