"""SQLAlchemy ORM models for the job queue (architecture/02 §9).

`Job.id` is `bigint GENERATED ALWAYS AS IDENTITY` — the deliberate exception to
the system-wide uuidv7 PK strategy (app/db/base.py:37-40).  Do not apply
`UUIDPrimaryKeyMixin` here.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class Job(Base, TimestampMixin):
    """One unit of async work (architecture/02 §9, architecture/12)."""

    __tablename__ = "jobs"
    __table_args__ = (
        # Partial indexes — declared here so alembic's autogenerate does not emit
        # spurious DROP INDEX. All five partial indexes use `postgresql_where`.
        # Storage params (fillfactor, autovacuum) are set via op.execute in the
        # migration; SQLAlchemy ORM has no portable column for those.
        sa.Index(
            "ix_jobs_claim",
            "queue", "priority", "run_at", "id",
            postgresql_where=sa.text("state = 'queued'"),
        ),
        sa.Index(
            "ix_jobs_lease",
            "lease_until",
            postgresql_where=sa.text("state = 'running'"),
        ),
        sa.Index(
            "ux_jobs_unique_key",
            "unique_key",
            unique=True,
            postgresql_where=sa.text("unique_key IS NOT NULL AND state IN ('queued', 'running')"),
        ),
        sa.Index(
            "ix_jobs_org_active",
            "organization_id",
            postgresql_where=sa.text("state IN ('queued', 'running')"),
        ),
        sa.Index(
            "ix_jobs_finished",
            "finished_at",
            postgresql_where=sa.text("state IN ('succeeded', 'failed', 'dead', 'cancelled')"),
        ),
    )

    # Deliberate bigint identity PK exception — architecture/02 §9.
    id: Mapped[int] = mapped_column(sa.BigInteger(), sa.Identity(always=True), primary_key=True)

    queue: Mapped[str] = mapped_column(sa.Text(), server_default="default", nullable=False)
    type: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False
    )
    state: Mapped[str] = mapped_column(
        sa.Text(),
        sa.CheckConstraint(
            "state IN ('queued','running','succeeded','failed','dead','cancelled')",
            name="ck_jobs_state",
        ),
        server_default="queued",
        nullable=False,
    )
    priority: Mapped[int] = mapped_column(
        sa.SmallInteger(), server_default=sa.text("100"), nullable=False
    )
    run_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )
    attempts: Mapped[int] = mapped_column(
        sa.Integer(), server_default=sa.text("0"), nullable=False
    )
    max_attempts: Mapped[int] = mapped_column(
        sa.Integer(), server_default=sa.text("5"), nullable=False
    )
    # Added alongside the columns referenced by the claim/heartbeat SQL but absent
    # from the original §9 DDL — see docs/phases/phase-03-background-jobs.md §Database changes.
    timeout_seconds: Mapped[int] = mapped_column(
        sa.Integer(), server_default=sa.text("60"), nullable=False
    )
    last_error: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    locked_by: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    unique_key: Mapped[str | None] = mapped_column(sa.Text(), nullable=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid(),
        nullable=True,
        # Deliberately no FK — survives org deletion for freeze-triggered
        # cancellation and tenant-scoped operational queries.
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )


class CronRun(Base):
    """Deduplication guard for the cron scheduler (architecture/02 §9).

    Each scheduler loop does `INSERT ... ON CONFLICT DO NOTHING RETURNING name`;
    a periodic job is enqueued only when a row was actually inserted — no leader
    election, correctness from the database.
    """

    __tablename__ = "cron_runs"
    __table_args__ = (sa.PrimaryKeyConstraint("name", "period_start", name="pk_cron_runs"),)

    name: Mapped[str] = mapped_column(sa.Text(), nullable=False)
    period_start: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
