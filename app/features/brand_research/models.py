"""brand_research_runs — architecture/02 §8 / architecture/08 §4."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BrandScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class BrandResearchRun(Base, UUIDPrimaryKeyMixin, TimestampMixin, BrandScopedMixin):
    __tablename__ = "brand_research_runs"

    requested_by: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(default=None)
    status: Mapped[str] = mapped_column(server_default="queued")
    job_id: Mapped[int | None] = mapped_column(BigInteger, default=None)
    decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_decisions.id", ondelete="SET NULL"), default=None
    )
    crawled_pages: Mapped[int | None] = mapped_column(Integer, default=None)
    extracted: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    proposal: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    applied_brand_version: Mapped[int | None] = mapped_column(Integer, default=None)
    error_message: Mapped[str | None] = mapped_column(default=None)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'partial', 'failed')",
            name="status_valid",
        ),
        ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            ondelete="CASCADE",
            name="fk_brand_research_runs_brand_org",
        ),
        Index(
            "ix_brand_research_runs_brand_created",
            "brand_id",
            "created_at",
        ),
        Index(
            "ix_brand_research_runs_brand_source",
            "brand_id",
            "source_url",
        ),
    )
