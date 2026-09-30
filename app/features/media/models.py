from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import (
    Base,
    BrandScopedMixin,
    CreatedAtMixin,
    SoftDeleteMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class UploadIntent(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """architecture/02 §4 — write-once until /complete; id becomes media_assets.id."""

    __tablename__ = "upload_intents"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    brand_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    r2_bucket: Mapped[str]
    r2_key: Mapped[str] = mapped_column(unique=True)
    original_filename: Mapped[str]
    content_type: Mapped[str]
    declared_size: Mapped[int] = mapped_column(BigInteger)
    presign_headers: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    expires_at: Mapped[datetime]
    completed_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_upload_intents_brand_org",
            ondelete="CASCADE",
        ),
        CheckConstraint("declared_size > 0", name="declared_size_positive"),
        Index(
            "ix_upload_intents_expiry",
            "expires_at",
            postgresql_where=text("completed_at IS NULL"),
        ),
    )


class MediaAsset(Base, UUIDPrimaryKeyMixin, TimestampMixin, BrandScopedMixin, SoftDeleteMixin):
    """architecture/02 §4 — created only at /complete."""

    __tablename__ = "media_assets"

    kind: Mapped[str]
    source: Mapped[str]
    status: Mapped[str] = mapped_column(server_default="ready")
    r2_bucket: Mapped[str]
    r2_key: Mapped[str] = mapped_column(unique=True)
    content_type: Mapped[str]
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    checksum_sha256: Mapped[str | None] = mapped_column(default=None)
    width: Mapped[int]
    height: Mapped[int]
    duration_seconds: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), default=None)
    poster_r2_key: Mapped[str | None] = mapped_column(default=None)
    alt_ar: Mapped[str] = mapped_column(server_default="")
    alt_en: Mapped[str] = mapped_column(server_default="")
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(TEXT), server_default=text("'{}'::text[]")
    )
    focal_x: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), default=None)
    focal_y: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), default=None)
    attribution: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    template_params: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    generation: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    ai_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_decisions.id", ondelete="SET NULL"), default=None
    )
    image_generation_output_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("image_generation_outputs.id", ondelete="SET NULL"), default=None
    )
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    original_filename: Mapped[str | None] = mapped_column(default=None)

    __table_args__ = (
        ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_media_assets_brand_org",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "brand_id", name="uq_media_assets_id_brand_id"),
        CheckConstraint("kind IN ('image', 'video', 'template')", name="kind_valid"),
        CheckConstraint(
            "source IN ('upload', 'stock', 'generated', 'template')", name="source_valid"
        ),
        CheckConstraint(
            "status IN ('ready', 'processing', 'failed')", name="status_valid"
        ),
        CheckConstraint("width > 0", name="width_positive"),
        CheckConstraint("height > 0", name="height_positive"),
        CheckConstraint(
            "(focal_x IS NULL) = (focal_y IS NULL)", name="focal_both_or_neither"
        ),
        CheckConstraint(
            "focal_x IS NULL OR (focal_x BETWEEN 0 AND 1 AND focal_y BETWEEN 0 AND 1)",
            name="focal_in_unit_square",
        ),
        CheckConstraint(
            "source <> 'stock' OR attribution IS NOT NULL",
            name="stock_requires_attribution",
        ),
        CheckConstraint(
            "(source = 'template') = (template_params IS NOT NULL)",
            name="template_params_iff_template",
        ),
        Index(
            "ix_media_brand_created",
            "brand_id",
            text("created_at DESC"),
            text("id DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_media_tags",
            "tags",
            postgresql_using="gin",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class MediaVariant(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """architecture/02 §4 — rendered derivatives."""

    __tablename__ = "media_variants"

    organization_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    media_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str]
    aspect: Mapped[str]
    platform: Mapped[str | None] = mapped_column(default=None)
    r2_key: Mapped[str] = mapped_column(unique=True)
    content_type: Mapped[str]
    width: Mapped[int]
    height: Mapped[int]
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    source_hash: Mapped[str]

    __table_args__ = (
        CheckConstraint(
            "purpose IN ('thumb', 'crop', 'render', 'poster')", name="purpose_valid"
        ),
        CheckConstraint(
            "aspect IN ('square', 'portrait', 'vertical', 'landscape', 'original')",
            name="aspect_valid",
        ),
        Index(
            "ux_media_variants",
            "media_asset_id",
            "purpose",
            "aspect",
            text("coalesce(platform, '')"),
            unique=True,
        ),
    )
