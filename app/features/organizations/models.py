from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Numeric, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Organization(Base, UUIDPrimaryKeyMixin, TimestampMixin, SoftDeleteMixin):
    """architecture/02 §2."""

    __tablename__ = "organizations"

    name: Mapped[str]
    industry: Mapped[str] = mapped_column(server_default="")
    city: Mapped[str] = mapped_column(server_default="")
    vat_number: Mapped[str | None] = mapped_column(default=None)
    timezone: Mapped[str] = mapped_column(server_default="Asia/Riyadh")
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )

    __table_args__ = (
        # A NULL vat_number satisfies a CHECK by Postgres's own three-valued
        # logic — no explicit "OR vat_number IS NULL" needed.
        CheckConstraint("vat_number ~ '^[0-9]{15}$'", name="vat_number_format"),
    )


class OrgSettings(Base):
    """architecture/02 §2. `updated_at` only, no `created_at` — a single row per
    organization, created alongside it, so a separate creation timestamp records
    nothing the parent row doesn't already have. A separate table (not columns on
    `organizations`) so a freeze, which takes `FOR UPDATE`, never locks the
    `organizations` row itself.
    """

    __tablename__ = "org_settings"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
    publishing_frozen: Mapped[bool] = mapped_column(server_default=text("false"))
    frozen_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    frozen_by_name: Mapped[str | None] = mapped_column(default=None)
    frozen_at: Mapped[datetime | None] = mapped_column(default=None)
    frozen_reason: Mapped[str | None] = mapped_column(default=None)
    approval_mode: Mapped[str] = mapped_column(server_default="always")
    risk_threshold: Mapped[float] = mapped_column(Numeric(4, 3), server_default="0.150")
    notification_prefs: Mapped[dict[str, object]] = mapped_column(
        JSONB, server_default=text("'{}'::jsonb")
    )
    onboarding: Mapped[dict[str, object]] = mapped_column(
        JSONB,
        server_default=text(
            '\'{"brand": false, "channel": false, "plan": false, "firstApproval": false}\'::jsonb'
        ),
    )
    business_profile: Mapped[dict[str, object] | None] = mapped_column(JSONB, default=None)

    __table_args__ = (
        CheckConstraint("approval_mode IN ('always', 'autonomous')", name="approval_mode_valid"),
        CheckConstraint("risk_threshold BETWEEN 0 AND 1", name="risk_threshold_range"),
    )
