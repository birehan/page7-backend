from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RateLimitWindow(Base):
    """architecture/02 §rate_limits. Deliberately not Redis — a fixed-window
    counter on Postgres, per ADR-0002's "no second stateful system" decision. No
    caller until Phase 2 wires login lockout in; buckets look like
    'login:email:<sha256>', 'login:ip:<ip>', 'route:<name>:<org>'.
    """

    __tablename__ = "rate_limits"

    bucket: Mapped[str] = mapped_column(primary_key=True)
    window_start: Mapped[datetime] = mapped_column(primary_key=True)
    count: Mapped[int] = mapped_column(default=1)
