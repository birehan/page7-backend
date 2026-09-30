from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, BrandScopedMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Strategy(Base, UUIDPrimaryKeyMixin, TimestampMixin, BrandScopedMixin):
    """architecture/02 §13 — one row per brand (UNIQUE brand_id)."""

    __tablename__ = "strategies"

    goals: Mapped[list[Any]] = mapped_column(JSONB, server_default=text("'[]'::jsonb"))
    cadence: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default=text("'{}'::jsonb"))
    version: Mapped[int] = mapped_column(server_default=text("1"))
    # Bare uuid until Phase 7 creates `ai_decisions` — same forward-ref pattern as
    # oauth_states.brand_id in Phase 2.
    generated_by_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ai_decisions.id", ondelete="SET NULL"), default=None
    )

    __table_args__ = (
        CheckConstraint("jsonb_typeof(goals) = 'array'", name="goals_array"),
        ForeignKeyConstraint(
            ["brand_id", "organization_id"],
            ["brands.id", "brands.organization_id"],
            name="fk_strategies_brand_org",
            ondelete="CASCADE",
        ),
        UniqueConstraint("brand_id", name="uq_strategies_brand_id"),
    )
