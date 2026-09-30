from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Computed,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    BrandScopedMixin,
    CreatedAtMixin,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

# IMMUTABLE Riyadh-day expression (UTC+3, no DST). `AT TIME ZONE` is STABLE and
# rejected in GENERATED columns — architecture/02 §shorthand <riyadh_day>.
_SCHEDULED_DATE_RIYADH = (
    "DATE '1970-01-01' + floor("
    "(extract(epoch from (scheduled_at - TIMESTAMPTZ '1970-01-01 00:00:00+00')) "
    "+ 10800) / 86400)::int"
)


class PostStatusTransition(Base):
    """Legal (from, to) pairs — enforced by service-layer CAS and a BEFORE UPDATE
    OF status trigger (architecture/02 §6, architecture/03).
    """

    __tablename__ = "post_status_transitions"

    from_status: Mapped[str] = mapped_column(primary_key=True)
    to_status: Mapped[str] = mapped_column(primary_key=True)


class Post(Base, UUIDPrimaryKeyMixin, TimestampMixin, BrandScopedMixin, SoftDeleteMixin):
    """architecture/02 §6 — one row per platform; group_id ties cross-posted siblings."""

    __tablename__ = "posts"

    group_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    platform: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="draft")
    title: Mapped[str | None] = mapped_column(default=None)
    internal_note: Mapped[str | None] = mapped_column(default=None)
    pillar_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    cultural_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cultural_events.id", ondelete="SET NULL"), default=None
    )
    is_paid: Mapped[bool] = mapped_column(server_default=text("false"))
    scheduled_at: Mapped[datetime]
    scheduled_date_riyadh: Mapped[date] = mapped_column(
        Date, Computed(_SCHEDULED_DATE_RIYADH, persisted=True)
    )
    schedule_epoch: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    first_comment: Mapped[str | None] = mapped_column(default=None)
    variants: Mapped[list[Any]] = mapped_column(
        JSONB, server_default=text("'[]'::jsonb")
    )
    risk: Mapped[dict[str, Any]] = mapped_column(
        # Space after colon so SQLAlchemy does not treat `:0` as a bind param.
        JSONB,
        server_default=text("""'{"score": 0, "reasons": []}'::jsonb"""),
    )
    risk_score: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), server_default=text("0")
    )
    change_request_reason: Mapped[str | None] = mapped_column(default=None)
    reject_reason: Mapped[str | None] = mapped_column(default=None)
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    submitted_at: Mapped[datetime | None] = mapped_column(default=None)
    approved_at: Mapped[datetime | None] = mapped_column(default=None)
    published_at: Mapped[datetime | None] = mapped_column(default=None)
    zernio_post_id: Mapped[str | None] = mapped_column(default=None)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    approved_via: Mapped[str | None] = mapped_column(default=None)
    approved_review_link_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("review_links.id", ondelete="SET NULL"), default=None
    )
    ai_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_decisions.id", ondelete="SET NULL"), default=None
    )
    brand_version: Mapped[int | None] = mapped_column(Integer, default=None)
    version: Mapped[int] = mapped_column(Integer, server_default=text("1"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_posts_brand_org",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["pillar_id", "brand_id"],
            ["content_pillars.id", "content_pillars.brand_id"],
            name="fk_posts_pillar_brand",
            ondelete="SET NULL",
        ),
        UniqueConstraint("id", "brand_id", name="uq_posts_id_brand_id"),
        CheckConstraint(
            "platform IN ('instagram', 'facebook', 'tiktok', 'snapchat', 'whatsapp')",
            name="platform_valid",
        ),
        CheckConstraint(
            "status IN ("
            "'idea', 'drafting', 'draft', 'in_review', 'changes_requested', "
            "'approved', 'scheduled', 'publishing', 'published', 'failed')",
            name="status_valid",
        ),
        CheckConstraint(
            "approved_via IS NULL OR approved_via IN ('user', 'policy', 'review_link')",
            name="approved_via_valid",
        ),
        CheckConstraint(
            "jsonb_typeof(variants) = 'array' AND jsonb_array_length(variants) <= 2",
            name="variants_array_max_2",
        ),
        CheckConstraint(
            "risk_score BETWEEN 0 AND 1", name="risk_score_unit_interval"
        ),
        CheckConstraint(
            "status <> 'published' OR published_at IS NOT NULL",
            name="published_requires_published_at",
        ),
        CheckConstraint(
            "status <> 'in_review' OR submitted_at IS NOT NULL",
            name="in_review_requires_submitted_at",
        ),
        Index(
            "ix_posts_brand_sched",
            "brand_id",
            "scheduled_at",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_posts_brand_status_sched",
            "brand_id",
            "status",
            "scheduled_at",
            "id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_posts_due",
            "scheduled_at",
            postgresql_where=text("status = 'scheduled' AND deleted_at IS NULL"),
        ),
        Index(
            "ix_posts_group",
            "group_id",
            postgresql_where=text("group_id IS NOT NULL"),
        ),
        Index(
            "ix_posts_publishing_stale",
            "updated_at",
            postgresql_where=text("status = 'publishing'"),
        ),
    )


class PostMedia(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Ordered join from a post to media_assets — architecture/02 §6."""

    __tablename__ = "post_media"

    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    brand_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    post_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    media_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="RESTRICT"), nullable=False
    )
    position: Mapped[int] = mapped_column(SmallInteger)
    alt_override: Mapped[str | None] = mapped_column(default=None)
    crop: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)

    __table_args__ = (
        ForeignKeyConstraint(
            ["post_id", "brand_id"],
            ["posts.id", "posts.brand_id"],
            name="fk_post_media_post_brand",
            ondelete="CASCADE",
        ),
        UniqueConstraint("post_id", "position", name="uq_post_media_post_id_position"),
        Index("ix_post_media_asset", "media_asset_id"),
    )


class PostVersion(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Append-only post snapshot trail — architecture/02 §6."""

    __tablename__ = "post_versions"

    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    brand_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    post_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer)
    post_row_version: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str]
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    author_ref: Mapped[str]
    author_name: Mapped[str]
    brand_version: Mapped[int | None] = mapped_column(Integer, default=None)
    ai_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_decisions.id", ondelete="SET NULL"), default=None
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)

    __table_args__ = (
        UniqueConstraint("post_id", "version", name="uq_post_versions_post_id_version"),
        CheckConstraint(
            "reason IN ('created', 'edited', 'ai_generated', 'changes_applied', 'restored')",
            name="reason_valid",
        ),
    )


class PostComment(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Internal comments on a post — architecture/02 §6."""

    __tablename__ = "post_comments"

    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    brand_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    post_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), nullable=False
    )
    author_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    author_name: Mapped[str]
    body: Mapped[str]
    lang: Mapped[str]
    resolved_at: Mapped[datetime | None] = mapped_column(default=None)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    __table_args__ = (
        CheckConstraint("lang IN ('ar', 'en')", name="lang_valid"),
        Index("ix_post_comments", "post_id", "created_at"),
    )
