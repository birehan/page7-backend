from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin


class Membership(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """architecture/02 §2. `last_brand_id` → brands(id) ON DELETE SET NULL
    (added in Phase 4 once `brands` existed).

    The last-owner protection rule (architecture/05 §3) is enforced in
    features/team/service.py, not here: `SELECT ... FOR UPDATE` on the parent
    `organizations` row, then a `COUNT(*) WHERE role = 'owner'` check, in the
    same transaction as the mutating `DELETE`/role `UPDATE`.
    """

    __tablename__ = "memberships"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str]
    last_brand_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("brands.id", ondelete="SET NULL"), default=None
    )
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    joined_at: Mapped[datetime] = mapped_column(server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "role IN ('owner', 'admin', 'editor', 'approver', 'viewer')", name="role_valid"
        ),
        UniqueConstraint("organization_id", "user_id"),
        Index("ix_memberships_user", "user_id"),
    )
