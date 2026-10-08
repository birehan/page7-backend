"""Add email_verification_challenges for signup / login email OTP.

Revision ID: 0024_email_verify_challenges
Revises: 0023_fix_ck_names
Create Date: 2026-10-01

Callers: auth service/repository. Schema: EmailVerificationChallenge.
User instruction: Implement the plan as specified (Signup email verification 6-digit OTP).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0024_email_verify_challenges"
down_revision: str | None = "0023_fix_ck_names"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_verification_challenges",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("code_salt", sa.Text(), nullable=False),
        sa.Column("code_hash", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("locale", sa.Text(), server_default="en", nullable=False),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.CheckConstraint(
            "locale IN ('ar', 'en')",
            name=op.f("ck_email_verification_challenges_locale_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_email_verification_challenges_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_verification_challenges")),
        sa.UniqueConstraint(
            "token_hash",
            name=op.f("uq_email_verification_challenges_token_hash"),
        ),
    )
    op.create_index(
        "ix_email_verify_user_active",
        "email_verification_challenges",
        ["user_id"],
        postgresql_where=sa.text("consumed_at IS NULL"),
    )
    op.create_index(
        "ix_email_verify_expiry",
        "email_verification_challenges",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_email_verify_expiry", table_name="email_verification_challenges")
    op.drop_index("ix_email_verify_user_active", table_name="email_verification_challenges")
    op.drop_table("email_verification_challenges")
