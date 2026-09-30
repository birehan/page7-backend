"""SQLAlchemy ORM models for the SSE-over-jobs bridge (architecture/02 §8,
architecture/12 §5).

`Run` and `RunEvent` ship with zero callers until Phase 7/8/11 — the same
"infrastructure ahead of its first feature consumer" pattern Phase 1 used for
`idempotency_keys`/`rate_limits`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


class Run(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A durable, bridge-backed long-running operation (brand_relearn,
    strategy, plan_commit, visuals_generate).  The backing job is enqueued in
    the same transaction as the row insert.
    """

    __tablename__ = "runs"
    __table_args__ = (
        sa.UniqueConstraint(
            "organization_id",
            "kind",
            "idempotency_key",
            name="uq_runs_org_kind_idempotency_key",
        ),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid(), nullable=False)
    # brand_id has no FK — brands doesn't exist until Phase 4
    brand_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    kind: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    request_hash: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    status: Mapped[str] = mapped_column(
        sa.Text(), server_default="queued", nullable=False
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB(), nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(postgresql.JSONB(), nullable=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid(), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class RunEvent(Base, CreatedAtMixin):
    """One SSE frame.  Appended by the worker; replayed from seq=1 on a resumed
    stream.  Purged 24 h after the run finishes.
    """

    __tablename__ = "run_events"
    __table_args__ = (
        sa.PrimaryKeyConstraint("run_id", "seq", name="pk_run_events"),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid(),
        sa.ForeignKey("runs.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(sa.Integer(), nullable=False)
    event: Mapped[dict[str, Any]] = mapped_column(postgresql.JSONB(), nullable=False)
