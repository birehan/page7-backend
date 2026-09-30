from __future__ import annotations

from sqlalchemy import Index
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class WaitlistSignup(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Pre-launch marketing capture — deliberately outside the org/user model:
    a waitlist row exists before an organization does, and never becomes one
    automatically. Onboarding a signup into a real account is a manual,
    separate step (invite) until self-serve signup reopens.
    """

    __tablename__ = "waitlist_signups"

    email: Mapped[str] = mapped_column(unique=True)
    business_name: Mapped[str | None] = mapped_column(default=None)
    city: Mapped[str | None] = mapped_column(default=None)
    phone: Mapped[str | None] = mapped_column(default=None)
    locale: Mapped[str | None] = mapped_column(default=None)
    source: Mapped[str | None] = mapped_column(default=None)

    __table_args__ = (Index("ix_waitlist_signups_created", "created_at"),)
