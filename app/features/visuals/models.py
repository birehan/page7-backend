"""image_generations / image_generation_outputs — architecture/02 §8."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    SmallInteger,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    BrandScopedMixin,
    CreatedAtMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)

_STYLE_VALUES = ("photo", "flat", "three-d", "minimal", "saudi-modern", "poster")
_ASPECT_VALUES = ("square", "portrait", "vertical", "landscape")
_STATUS_VALUES = ("queued", "running", "succeeded", "failed", "partial")


class ImageGeneration(Base, UUIDPrimaryKeyMixin, TimestampMixin, BrandScopedMixin):
    """One row per /visuals/generate batch request."""

    __tablename__ = "image_generations"

    requested_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Nullable until the run finalizes — ai_decisions has no in-flight status,
    # and insert_decision meters usage on insert (architecture/02 §8 as amended
    # by Phase 11; mirrors brand_research_runs.decision_id).
    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_decisions.id", ondelete="SET NULL"), default=None
    )
    provider: Mapped[str] = mapped_column(server_default="fal")
    model: Mapped[str]
    prompt: Mapped[str]
    negative_prompt: Mapped[str | None] = mapped_column(default=None)
    style: Mapped[str]
    aspect: Mapped[str]
    count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    use_brand_colors: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), nullable=False
    )
    status: Mapped[str] = mapped_column(server_default="queued")
    provider_request_id: Mapped[str | None] = mapped_column(default=None)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), default=None)
    latency_ms: Mapped[int | None] = mapped_column(default=None)
    error_code: Mapped[str | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    __table_args__ = (
        CheckConstraint(
            "style IN (" + ", ".join(f"'{v}'" for v in _STYLE_VALUES) + ")",
            name="style_valid",
        ),
        CheckConstraint(
            "aspect IN (" + ", ".join(f"'{v}'" for v in _ASPECT_VALUES) + ")",
            name="aspect_valid",
        ),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _STATUS_VALUES) + ")",
            name="status_valid",
        ),
        CheckConstraint("count BETWEEN 1 AND 4", name="count_range"),
        ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_image_generations_brand_org",
            ondelete="CASCADE",
        ),
        Index(
            "ix_image_generations_brand_created",
            "brand_id",
            text("created_at DESC"),
        ),
    )


class ImageGenerationOutput(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One row per image produced (or attempted) in a generation batch."""

    __tablename__ = "image_generation_outputs"

    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    image_generation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("image_generations.id", ondelete="CASCADE"), nullable=False
    )
    index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    provider_url: Mapped[str]
    provider_url_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    width: Mapped[int]
    height: Mapped[int]
    seed: Mapped[int | None] = mapped_column(BigInteger, default=None)
    r2_key: Mapped[str | None] = mapped_column(default=None)
    kept_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )

    __table_args__ = (
        UniqueConstraint(
            "image_generation_id",
            "index",
            name="uq_image_generation_outputs_gen_index",
        ),
        CheckConstraint("width > 0", name="width_positive"),
        CheckConstraint("height > 0", name="height_positive"),
        CheckConstraint('"index" >= 0', name="index_nonnegative"),
        Index(
            "ix_image_generation_outputs_r2_key",
            "r2_key",
            postgresql_where=text("r2_key IS NOT NULL"),
        ),
    )
