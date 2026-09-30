"""Initial migration: idempotency_keys, rate_limits

Both tables are empty and have no caller yet — Phase 6 wires the Idempotency-Key
dependency in; Phase 2 wires login-lockout rate limiting in. They're created now
so those phases add a call site to existing infrastructure rather than also
owning a migration for it.

Revision ID: 0001_initial
Revises:
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limits",
        sa.Column("bucket", sa.Text(), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("bucket", "window_start", name=op.f("pk_rate_limits")),
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", postgresql.JSONB(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("organization_id", "key", name=op.f("pk_idempotency_keys")),
    )
    op.create_index(
        "ix_idempotency_expiry", "idempotency_keys", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_idempotency_expiry", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")
    op.drop_table("rate_limits")
