"""Seed billing plans, invoice counter, and backfill subscriptions (Phase 13).

Seeds the three catalog plans, the single global invoice_counters row, and one
trialing Growth subscription per live organization so GET /billing/subscription
never 404s after the flip.

Revision ID: 0017_seed_billing
Revises: 0016_billing
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_seed_billing"
down_revision: str | None = "0016_billing"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            INSERT INTO plans (
                code, name, price_monthly_sar, ai_generations_monthly,
                brands_limit, seats_limit, channels_limit, active
            ) VALUES
                ('starter', 'Starter', 599.00, 200, 1, 5, 2, true),
                ('growth',  'Growth',  1499.00, 800, 3, 15, 3, true),
                ('agency',  'Agency',  3499.00, 3000, 999999, 999999, 999999, true)
            ON CONFLICT (code) DO NOTHING
            """
        )
    )
    op.execute(
        sa.text(
            """
            INSERT INTO invoice_counters (issuer, last_number)
            VALUES ('pgblank', 0)
            ON CONFLICT (issuer) DO NOTHING
            """
        )
    )
    # One live Growth trial per org that does not already have a live subscription.
    # Growth (brands_limit=3) avoids putting every existing 1-brand org at the
    # brand limit the moment the flip lands.
    op.execute(
        sa.text(
            """
            INSERT INTO subscriptions (
                organization_id, plan_code, status, price_monthly_sar,
                current_period_start, current_period_end, trial_ends_at
            )
            SELECT
                o.id,
                'growth',
                'trialing',
                1499.00,
                now(),
                now() + interval '1 month',
                now() + interval '14 days'
            FROM organizations o
            WHERE o.deleted_at IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM subscriptions s
                WHERE s.organization_id = o.id
                  AND s.status IN ('trialing', 'active', 'past_due')
              )
            """
        )
    )


def downgrade() -> None:
    raise NotImplementedError("0017_seed_billing is forward-only")
