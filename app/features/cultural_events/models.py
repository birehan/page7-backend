from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, CheckConstraint, Date, ForeignKey, Index, SmallInteger, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class CulturalEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """architecture/02 §12 — global catalog, not tenant-scoped."""

    __tablename__ = "cultural_events"

    slug: Mapped[str] = mapped_column(unique=True)
    name: Mapped[str]
    name_ar: Mapped[str]
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    kind: Mapped[str]
    hijri_year: Mapped[int | None] = mapped_column(SmallInteger, default=None)
    source: Mapped[str]
    enabled_by_default: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))

    __table_args__ = (
        CheckConstraint("kind IN ('religious', 'national', 'seasonal')", name="kind_valid"),
        CheckConstraint("source IN ('umm_al_qura', 'manual')", name="source_valid"),
        CheckConstraint("end_date >= start_date", name="end_after_start"),
        Index("ix_cultural_events_start", "start_date"),
    )


class OrganizationCulturalEventSetting(Base):
    """architecture/02 §12 — per-org enable/disable overlay on the global catalog."""

    __tablename__ = "organization_cultural_event_settings"

    organization_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    cultural_event_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cultural_events.id", ondelete="CASCADE"), primary_key=True
    )
    enabled: Mapped[bool]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
