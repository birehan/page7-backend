from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    text,
)
from sqlalchemy.dialects.postgresql import INET
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BrandScopedMixin, CreatedAtMixin, UUIDPrimaryKeyMixin


class ReviewLink(Base, UUIDPrimaryKeyMixin, CreatedAtMixin, BrandScopedMixin):
    """architecture/02 §7 — guest review token; plaintext returned once, never stored."""

    __tablename__ = "review_links"

    token_hash: Mapped[str] = mapped_column(unique=True)
    locale: Mapped[str]
    created_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None] = mapped_column(default=None)
    decision: Mapped[str | None] = mapped_column(default=None)
    decided_at: Mapped[datetime | None] = mapped_column(default=None)
    decided_by_name: Mapped[str | None] = mapped_column(default=None)
    decision_comment: Mapped[str | None] = mapped_column(default=None)
    decided_ip: Mapped[str | None] = mapped_column(INET, default=None)
    view_count: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    last_viewed_at: Mapped[datetime | None] = mapped_column(default=None)

    __table_args__ = (
        ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_review_links_brand_org",
            ondelete="CASCADE",
        ),
        CheckConstraint("locale IN ('ar', 'en')", name="locale_valid"),
        CheckConstraint(
            "decision IS NULL OR decision IN ('approved', 'changes_requested')",
            name="decision_valid",
        ),
        CheckConstraint(
            "decision IS NULL OR decided_at IS NOT NULL",
            name="decision_requires_decided_at",
        ),
    )


class ReviewLinkPost(Base):
    """Frozen group snapshot at review-link creation — architecture/02 §7."""

    __tablename__ = "review_link_posts"

    review_link_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("review_links.id", ondelete="CASCADE"), primary_key=True
    )
    post_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("posts.id", ondelete="CASCADE"), primary_key=True
    )

    __table_args__ = (Index("ix_review_link_posts", "post_id"),)
