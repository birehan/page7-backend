from __future__ import annotations

import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TenantMixin, UUIDPrimaryKeyMixin


class AuditLog(Base, UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin):
    """architecture/02 §11. Append-only at the database level — the runtime DB
    role has no UPDATE or DELETE grant on this table (architecture/05 §11); that
    grant lands with the rest of the three-role DB access work, not this phase.

    `GET /audit` is a UNION ALL of this table and `ai_decisions`, merged in the
    application layer — this phase only ever writes `kind='audit'` rows;
    `ai_decisions` (and its `kind='ai'` half of the feed) starts in Phase 7.
    """

    __tablename__ = "audit_logs"

    brand_id: Mapped[uuid.UUID | None] = mapped_column(default=None)
    actor_kind: Mapped[str]
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    actor_ref: Mapped[str]
    actor_name: Mapped[str]
    action: Mapped[str]
    target_type: Mapped[str]
    target_id: Mapped[uuid.UUID]
    meta: Mapped[dict[str, object] | None] = mapped_column(JSONB, default=None)
    ip: Mapped[str | None] = mapped_column(INET, default=None)
    request_id: Mapped[str | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint(
            "actor_kind IN ('user', 'system', 'policy', 'guest')", name="actor_kind_valid"
        ),
        Index(
            "ix_audit_org_created",
            "organization_id",
            text("created_at DESC"),
            text("id DESC"),
        ),
        Index(
            "ix_audit_target",
            "organization_id",
            "target_type",
            "target_id",
            text("created_at DESC"),
        ),
        Index("ix_audit_created_brin", "created_at", postgresql_using="brin"),
    )
