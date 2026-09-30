"""metric_snapshots / insight_reports / analytics_sync_state — architecture/02 §14."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
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

from app.db.base import Base, BrandScopedMixin, CreatedAtMixin, UUIDPrimaryKeyMixin

_PLATFORM_VALUES = ("instagram", "facebook", "tiktok", "snapchat", "whatsapp")
_SYNC_STATUS_VALUES = ("ok", "error", "gated")


class MetricSnapshot(Base, BrandScopedMixin):
    """Daily post- or account-level metrics row — bigint identity PK."""

    __tablename__ = "metric_snapshots"

    id: Mapped[int] = mapped_column(
        BigInteger(), Identity(always=True), primary_key=True
    )
    social_account_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("social_accounts.id"), nullable=False
    )
    platform: Mapped[str]
    post_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), default=None
    )
    metric_date: Mapped[date] = mapped_column(Date, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    impressions: Mapped[int | None] = mapped_column(BigInteger, default=None)
    reach: Mapped[int | None] = mapped_column(BigInteger, default=None)
    likes: Mapped[int | None] = mapped_column(Integer, default=None)
    comments: Mapped[int | None] = mapped_column(Integer, default=None)
    shares: Mapped[int | None] = mapped_column(Integer, default=None)
    saves: Mapped[int | None] = mapped_column(Integer, default=None)
    video_views: Mapped[int | None] = mapped_column(BigInteger, default=None)
    clicks: Mapped[int | None] = mapped_column(Integer, default=None)
    followers: Mapped[int | None] = mapped_column(Integer, default=None)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)

    __table_args__ = (
        CheckConstraint(
            "platform IN (" + ", ".join(f"'{v}'" for v in _PLATFORM_VALUES) + ")",
            name="platform_valid",
        ),
        ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_metric_snapshots_brand_org",
            ondelete="CASCADE",
        ),
        Index(
            "ux_metric_post",
            "post_id",
            "metric_date",
            unique=True,
            postgresql_where=text("post_id IS NOT NULL"),
        ),
        Index(
            "ux_metric_account",
            "social_account_id",
            "metric_date",
            unique=True,
            postgresql_where=text("post_id IS NULL"),
        ),
        Index("ix_metric_brand_date", "brand_id", "metric_date"),
        Index(
            "ix_metric_captured_brin",
            "captured_at",
            postgresql_using="brin",
        ),
    )


class InsightReport(Base, UUIDPrimaryKeyMixin, CreatedAtMixin, BrandScopedMixin):
    """Weekly LLM-narrated insight document — one per brand per Riyadh week."""

    __tablename__ = "insight_reports"

    week_of: Mapped[date] = mapped_column(Date, nullable=False)
    what_happened: Mapped[str]
    why: Mapped[str]
    what_to_change: Mapped[str]
    next_actions: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("'{}'::text[]")
    )
    top_post_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("posts.id", ondelete="SET NULL"), default=None
    )
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    series: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    pillar_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    platform_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_decisions.id"), default=None
    )
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_insight_reports_brand_org",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "brand_id",
            "week_of",
            name="uq_insight_reports_brand_id_week_of",
        ),
    )


class AnalyticsSyncState(Base):
    """Credential-scoped analytics cursor — not tenant-scoped."""

    __tablename__ = "analytics_sync_state"

    credential_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("zernio_credentials.id", ondelete="CASCADE"), primary_key=True
    )
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
    last_cursor: Mapped[str | None] = mapped_column(default=None)
    bootstrapped_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    last_sync_status: Mapped[str] = mapped_column(server_default="ok")
    last_error: Mapped[str | None] = mapped_column(default=None)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, server_default=text("0")
    )

    __table_args__ = (
        CheckConstraint(
            "last_sync_status IN ("
            + ", ".join(f"'{v}'" for v in _SYNC_STATUS_VALUES)
            + ")",
            name="last_sync_status_valid",
        ),
    )
