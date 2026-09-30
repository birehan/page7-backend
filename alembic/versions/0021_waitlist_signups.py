"""Add waitlist_signups for the pre-launch marketing landing page.

Revision ID: 0021_waitlist_signups
Revises: 0020_poster_style
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0021_waitlist_signups"
down_revision: str | None = "0020_poster_style"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "waitlist_signups",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("business_name", sa.Text(), nullable=True),
        sa.Column("city", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("locale", sa.Text(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_waitlist_signups")),
        sa.UniqueConstraint("email", name=op.f("uq_waitlist_signups_email")),
    )
    op.create_index(
        "ix_waitlist_signups_created",
        "waitlist_signups",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_waitlist_signups_created", table_name="waitlist_signups")
    op.drop_table("waitlist_signups")
