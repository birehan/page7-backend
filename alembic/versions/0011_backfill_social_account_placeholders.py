"""Phase 9 data migration: backfill social_account placeholders.

Revision ID: 0011_backfill_social_accts
Revises: 0010_social_accounts_zernio
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# alembic_version.version_num is varchar(32) — keep the id ≤ 32 chars.
revision: str = "0011_backfill_social_accts"
down_revision: str | None = "0010_social_accounts_zernio"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_BATCH_BRANDS = 5000

_BACKFILL_SQL = sa.text(
    """
    WITH batch_brands AS (
        SELECT b.id, b.organization_id
        FROM brands b
        WHERE b.deleted_at IS NULL
          AND EXISTS (
              SELECT 1
              FROM (
                  VALUES
                      ('instagram'),
                      ('facebook'),
                      ('tiktok'),
                      ('snapchat'),
                      ('whatsapp')
              ) AS p(platform)
              WHERE NOT EXISTS (
                  SELECT 1
                  FROM social_accounts sa
                  WHERE sa.brand_id = b.id
                    AND sa.platform = p.platform
              )
          )
        ORDER BY b.id
        LIMIT :batch
    )
    INSERT INTO social_accounts (
        organization_id, brand_id, platform, status, handle
    )
    SELECT
        bb.organization_id,
        bb.id,
        p.platform,
        'disconnected',
        ''
    FROM batch_brands bb
    CROSS JOIN (
        VALUES
            ('instagram'),
            ('facebook'),
            ('tiktok'),
            ('snapchat'),
            ('whatsapp')
    ) AS p(platform)
    WHERE NOT EXISTS (
        SELECT 1
        FROM social_accounts sa
        WHERE sa.brand_id = bb.id
          AND sa.platform = p.platform
    )
    RETURNING id
    """
)


def upgrade() -> None:
    """Insert five placeholder rows per brand missing any platform.

    Batched in loops of ~5000 brands per short transaction
    (architecture/03 expand/contract data-migration pattern). Idempotent via
    NOT EXISTS so re-running a partial batch is safe.
    """
    conn = op.get_bind()
    while True:
        result = conn.execute(_BACKFILL_SQL, {"batch": _BATCH_BRANDS})
        inserted = result.fetchall()
        if not inserted:
            break
        conn.commit()


def downgrade() -> None:
    raise NotImplementedError("data migrations are forward-only")
