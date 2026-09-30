from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
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
    TenantMixin,
    TimestampMixin,
    UUIDPrimaryKeyMixin,
)


class Brand(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin, SoftDeleteMixin):
    """architecture/02 §3."""

    __tablename__ = "brands"

    name: Mapped[str]
    industry: Mapped[str] = mapped_column(server_default="")
    city: Mapped[str] = mapped_column(server_default="")
    website: Mapped[str | None] = mapped_column(default=None)
    logo_url: Mapped[str | None] = mapped_column(default=None)
    guidelines: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    version: Mapped[int] = mapped_column(server_default=text("1"))

    __table_args__ = (
        UniqueConstraint("id", "organization_id", name="uq_brands_id_organization_id"),
        CheckConstraint("jsonb_typeof(guidelines) = 'object'", name="guidelines_object"),
        Index(
            "ux_brands_org_name",
            "organization_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class BrandVersion(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Append-only brand snapshot trail — architecture/02 §3 / Versioning history."""

    __tablename__ = "brand_versions"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    brand_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int]
    reason: Mapped[str]
    author_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(
            "reason IN ('generated', 'edited', 'ai_relearn', 'restored')",
            name="reason_valid",
        ),
        UniqueConstraint("brand_id", "version", name="uq_brand_versions_brand_id_version"),
    )


class ContentPillar(Base, UUIDPrimaryKeyMixin, TimestampMixin, BrandScopedMixin, SoftDeleteMixin):
    """architecture/02 §3 — real table (not JSONB) because posts.pillar_id is a FK."""

    __tablename__ = "content_pillars"

    name: Mapped[str]
    description: Mapped[str] = mapped_column(server_default="")
    weight: Mapped[Decimal] = mapped_column(Numeric(5, 2), server_default="1")
    position: Mapped[int] = mapped_column(SmallInteger, server_default=text("0"))

    __table_args__ = (
        ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_content_pillars_brand_org",
            ondelete="CASCADE",
        ),
        UniqueConstraint("id", "brand_id", name="uq_content_pillars_id_brand_id"),
        CheckConstraint("weight >= 0", name="weight_non_negative"),
        Index(
            "ux_pillars_brand_name",
            "brand_id",
            text("lower(name)"),
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


class BrandCompetitor(Base, UUIDPrimaryKeyMixin, TimestampMixin, BrandScopedMixin):
    """architecture/02 §3 — no soft delete; nothing references it by FK."""

    __tablename__ = "brand_competitors"

    handle: Mapped[str]
    platform: Mapped[str]

    __table_args__ = (
        ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_brand_competitors_brand_org",
            ondelete="CASCADE",
        ),
        Index(
            "ux_brand_competitors_brand_platform_handle",
            "brand_id",
            "platform",
            text("lower(handle)"),
            unique=True,
        ),
    )
