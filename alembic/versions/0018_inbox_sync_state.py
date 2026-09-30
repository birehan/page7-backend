"""Add inbox_sync_state for per-account inbox backfill cursors.

Revision ID: 0018_inbox_sync_state
Revises: 0017_seed_billing
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0018_inbox_sync_state"
down_revision: str | None = "0017_seed_billing"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "inbox_sync_state",
        sa.Column(
            "social_account_id",
            sa.Uuid(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("dm_cursor", sa.Text(), nullable=True),
        sa.Column(
            "last_synced_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "last_sync_status",
            sa.Text(),
            server_default="ok",
            nullable=False,
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "consecutive_failures",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "last_sync_status IN ('ok', 'error', 'gated')",
            name=op.f("ck_inbox_sync_state_last_sync_status_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["social_account_id"],
            ["social_accounts.id"],
            name=op.f("fk_inbox_sync_state_social_account_id_social_accounts"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "social_account_id", name=op.f("pk_inbox_sync_state")
        ),
    )


def downgrade() -> None:
    op.drop_table("inbox_sync_state")
