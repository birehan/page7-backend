from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class IdempotencyKey(Base):
    """architecture/02 §idempotency_keys. No caller until Phase 6 wires the
    `Idempotency-Key` header dependency into mutating endpoints; the table exists
    now so that migration is additive, not foundational, when it lands.

    No foreign key to `organizations` yet — that table doesn't exist until Phase 2.
    """

    __tablename__ = "idempotency_keys"

    organization_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(primary_key=True)
    created_at: Mapped[datetime]
    scope: Mapped[str]  # HTTP method + route template
    request_hash: Mapped[str]
    response_status: Mapped[int | None] = mapped_column(default=None)
    response_body: Mapped[dict[str, object] | None] = mapped_column(JSONB, default=None)
    expires_at: Mapped[datetime]  # 24 hours from creation

    __table_args__ = (Index("ix_idempotency_expiry", "expires_at"),)
