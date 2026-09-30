"""Background jobs: Postgres-native queue (Phase 3)

`jobs`, `cron_runs` (architecture/02 §9, architecture/12),
`runs`, `run_events` (architecture/02 §8, architecture/12 §5).

Two deliberate divergences from the raw §9 shorthand, documented in
docs/phases/phase-03-background-jobs.md §Database changes:
  1. `jobs` gains `timeout_seconds` and `started_at` columns — both are
     referenced by the claim/heartbeat SQL in architecture/12 but absent from
     §9's DDL.
  2. `ix_jobs_claim` is the four-column §9 form `(queue, priority, run_at, id)`
     — the three-column form in architecture/12 is an abbreviated restatement.

`jobs.id` is `bigint GENERATED ALWAYS AS IDENTITY` — the deliberate exception
to the project-wide uuidv7 PK strategy (ADR-0009; documented in app/db/base.py).

All four tables are new and empty at deploy time, so no index needs CONCURRENTLY.

Revision ID: 0003_background_jobs
Revises: 0002_identity_tenancy
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_background_jobs"
down_revision: str | None = "0002_identity_tenancy"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # runs — SSE-over-jobs bridge (architecture/02 §8, architecture/12 §5)
    # Must be created before run_events (which has a FK to it).
    # ------------------------------------------------------------------
    op.create_table(
        "runs",
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("brand_id", sa.Uuid(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error", postgresql.JSONB(), nullable=True),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_runs"),
        sa.UniqueConstraint(
            "organization_id",
            "kind",
            "idempotency_key",
            name="uq_runs_org_kind_idempotency_key",
        ),
    )

    # ------------------------------------------------------------------
    # run_events — one SSE frame per row (architecture/02 §8)
    # ------------------------------------------------------------------
    op.create_table(
        "run_events",
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("run_id", "seq", name="pk_run_events"),
    )

    # ------------------------------------------------------------------
    # jobs — the queue table (architecture/02 §9, architecture/12)
    # Storage params are set via ALTER TABLE after creation so they are
    # always applied regardless of ORM-generated CREATE TABLE variance.
    # ------------------------------------------------------------------
    op.create_table(
        "jobs",
        # Bigint identity PK — deliberate exception to uuidv7 (ADR-0009).
        sa.Column(
            "id",
            sa.BigInteger(),
            sa.Identity(always=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("queue", sa.Text(), server_default="default", nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column(
            "state",
            sa.Text(),
            sa.CheckConstraint(
                "state IN ('queued','running','succeeded','failed','dead','cancelled')",
                name="ck_jobs_state",
            ),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("priority", sa.SmallInteger(), server_default=sa.text("100"), nullable=False),
        sa.Column(
            "run_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        # Added: referenced by claim/heartbeat SQL but absent from the written DDL
        # (see phase-03 §Database changes).
        sa.Column(
            "timeout_seconds", sa.Integer(), server_default=sa.text("60"), nullable=False
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        # Added: set in the claim UPDATE (see phase-03 §Database changes).
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("unique_key", sa.Text(), nullable=True),
        sa.Column(
            "organization_id",
            sa.Uuid(),
            nullable=True,
            # No FK — survives org deletion for freeze-triggered cancellation
            # and tenant-scoped operational queries (architecture/02 §9).
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Storage parameters: frequent UPDATE/DELETE on a hot table; fillfactor
    # leaves room for HOT updates; aggressive autovacuum keeps bloat controlled.
    op.execute(
        "ALTER TABLE jobs SET ("
        "fillfactor = 70, "
        "autovacuum_vacuum_scale_factor = 0.01, "
        "autovacuum_analyze_scale_factor = 0.02"
        ")"
    )

    # Partial indexes (hand-written — autogenerate cannot diff these reliably).
    # ix_jobs_claim: four-column form from architecture/02 §9; supports the
    # claim query's ORDER BY priority, run_at with id as tiebreak.
    op.create_index(
        "ix_jobs_claim",
        "jobs",
        ["queue", "priority", "run_at", "id"],
        postgresql_where=sa.text("state = 'queued'"),
    )
    op.create_index(
        "ix_jobs_lease",
        "jobs",
        ["lease_until"],
        postgresql_where=sa.text("state = 'running'"),
    )
    op.create_index(
        "ux_jobs_unique_key",
        "jobs",
        ["unique_key"],
        unique=True,
        postgresql_where=sa.text(
            "unique_key IS NOT NULL AND state IN ('queued', 'running')"
        ),
    )
    op.create_index(
        "ix_jobs_org_active",
        "jobs",
        ["organization_id"],
        postgresql_where=sa.text("state IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_jobs_finished",
        "jobs",
        ["finished_at"],
        postgresql_where=sa.text(
            "state IN ('succeeded', 'failed', 'dead', 'cancelled')"
        ),
    )

    # ------------------------------------------------------------------
    # cron_runs — scheduler deduplication guard (architecture/02 §9)
    # ------------------------------------------------------------------
    op.create_table(
        "cron_runs",
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", "period_start", name="pk_cron_runs"),
    )


def downgrade() -> None:
    op.drop_table("cron_runs")
    op.drop_index("ix_jobs_finished", table_name="jobs")
    op.drop_index("ix_jobs_org_active", table_name="jobs")
    op.drop_index("ux_jobs_unique_key", table_name="jobs")
    op.drop_index("ix_jobs_lease", table_name="jobs")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("run_events")
    op.drop_table("runs")
