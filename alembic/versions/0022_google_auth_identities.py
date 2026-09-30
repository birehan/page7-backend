"""Add user_identities and auth_oauth_states for Google OIDC login.

Revision ID: 0022_google_auth_identities
Revises: 0021_waitlist_signups
Create Date: 2026-09-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0022_google_auth_identities"
down_revision: str | None = "0021_waitlist_signups"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_identities",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("provider_subject", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=True),
        sa.CheckConstraint("provider IN ('google')", name="ck_user_identities_provider_valid"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_identities_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_identities")),
        sa.UniqueConstraint(
            "provider",
            "provider_subject",
            name="uq_user_identities_provider_subject",
        ),
    )
    op.create_index("ix_user_identities_user", "user_identities", ["user_id"])

    op.create_table(
        "auth_oauth_states",
        sa.Column("state_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("nonce", sa.Text(), nullable=False),
        sa.Column("remember_me", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("locale", sa.Text(), server_default="en", nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("locale IN ('ar', 'en')", name="ck_auth_oauth_states_locale_valid"),
        sa.PrimaryKeyConstraint("state_hash", name=op.f("pk_auth_oauth_states")),
    )
    op.create_index(
        "ix_auth_oauth_states_expiry",
        "auth_oauth_states",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_auth_oauth_states_expiry", table_name="auth_oauth_states")
    op.drop_table("auth_oauth_states")
    op.drop_index("ix_user_identities_user", table_name="user_identities")
    op.drop_table("user_identities")
