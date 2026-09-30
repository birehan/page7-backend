"""SQLAlchemy models for Phase 7 AI tables (architecture/02 §8/§15)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
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

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin

_KIND_VALUES = (
    "captions",
    "plan",
    "plan_commit",
    "brand_generate",
    "brand_relearn",
    "strategy",
    "alt_text",
    "image_generate",
    "reply_suggest",
    "sentiment",
    "insight",
)
_PROVIDER_VALUES = ("openai", "anthropic", "fal", "internal")
_STATUS_VALUES = ("succeeded", "failed", "partial")


class AiDecision(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Append-only AI decision ledger — architecture/02 §8."""

    __tablename__ = "ai_decisions"
    __table_args__ = (
        CheckConstraint(
            "kind IN (" + ", ".join(f"'{v}'" for v in _KIND_VALUES) + ")",
            name="kind_valid",
        ),
        CheckConstraint(
            "provider IN (" + ", ".join(f"'{v}'" for v in _PROVIDER_VALUES) + ")",
            name="provider_valid",
        ),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _STATUS_VALUES) + ")",
            name="status_valid",
        ),
        ForeignKeyConstraint(
            ["brand_id", "brand_version"],
            ["brand_versions.brand_id", "brand_versions.version"],
            name="fk_ai_decisions_brand_version",
            ondelete="SET NULL",
        ),
        Index(
            "ix_aidec_org_created",
            "organization_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index("ix_aidec_inputs_hash", "inputs_hash"),
        Index("ix_aidec_target", "target_type", "target_id"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    brand_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brands.id", ondelete="SET NULL"), default=None
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    kind: Mapped[str]
    provider: Mapped[str]
    model: Mapped[str]
    prompt_version: Mapped[str]
    inputs_hash: Mapped[str]
    input_summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    risk_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), default=None)
    target_type: Mapped[str]
    target_id: Mapped[uuid.UUID]
    brand_version: Mapped[int | None] = mapped_column(Integer, default=None)
    parent_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_decisions.id", ondelete="SET NULL"), default=None
    )
    status: Mapped[str]
    error_code: Mapped[str | None] = mapped_column(default=None)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, default=None)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), default=None)
    latency_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    provider_request_id: Mapped[str | None] = mapped_column(default=None)
    provider_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)


class AiFeedback(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Thumbs up/down on a generated variant — upserted on the unique key."""

    __tablename__ = "ai_feedback"
    __table_args__ = (
        CheckConstraint("rating IN ('up', 'down')", name="rating_valid"),
        UniqueConstraint(
            "decision_id",
            "variant_index",
            "user_id",
            name="uq_ai_feedback_decision_variant_user",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    decision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ai_decisions.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    variant_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    rating: Mapped[str]


class AiUsageCounter(Base):
    """Per-org per-month usage counter — upserted with every ai_decisions insert."""

    __tablename__ = "ai_usage_counters"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    period_month: Mapped[date] = mapped_column(Date, primary_key=True)
    generations: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    prompt_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    completion_tokens: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), server_default=text("0"))
    updated_at: Mapped[datetime] = mapped_column(server_default=text("now()"))
