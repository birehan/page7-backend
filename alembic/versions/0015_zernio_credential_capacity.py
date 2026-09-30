"""Zernio credential capacity + multi-profile-per-brand (failover).

- Adds max_accounts on zernio_credentials (Zernio free-key slot cap, default 2).
- Replaces ux_zprofile_brand_live (one profile per brand) with
  ux_zprofile_brand_credential_live (one live profile per brand+credential)
  so a brand can spill across keys when one key is at capacity.

Revision ID: 0015_zernio_credential_capacity
Revises: 0014_analytics_inbox
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_zernio_credential_capacity"
down_revision: str | None = "0014_analytics_inbox"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "zernio_credentials",
        sa.Column(
            "max_accounts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("2"),
        ),
    )

    op.drop_index("ux_zprofile_brand_live", table_name="zernio_profiles")
    op.create_index(
        "ux_zprofile_brand_credential_live",
        "zernio_profiles",
        ["brand_id", "credential_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    # Keep one live profile per brand so ux_zprofile_brand_live can be restored.
    op.execute(
        sa.text(
            """
            UPDATE zernio_profiles AS zp
            SET deleted_at = now()
            WHERE zp.deleted_at IS NULL
              AND zp.id NOT IN (
                SELECT kept.id
                FROM (
                  SELECT DISTINCT ON (brand_id) id
                  FROM zernio_profiles
                  WHERE deleted_at IS NULL
                  ORDER BY brand_id, created_at ASC
                ) AS kept
              )
            """
        )
    )
    op.drop_index(
        "ux_zprofile_brand_credential_live",
        table_name="zernio_profiles",
    )
    op.create_index(
        "ux_zprofile_brand_live",
        "zernio_profiles",
        ["brand_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.drop_column("zernio_credentials", "max_accounts")
