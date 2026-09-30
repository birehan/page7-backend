"""Data-seed smoke for 0017_seed_billing."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.billing.models import InvoiceCounter, Plan, Subscription
from app.features.organizations.models import Organization


async def test_0017_plans_and_counter_seeded(db_session: AsyncSession) -> None:
    for code in ("starter", "growth", "agency"):
        assert await db_session.get(Plan, code) is not None
    counter = await db_session.get(InvoiceCounter, "pgblank")
    assert counter is not None
    assert counter.last_number >= 0


async def test_0017_backfill_inserts_growth_trial(
    db_session: AsyncSession,
) -> None:
    """Re-runs the migration's INSERT…SELECT for a lone org with no subscription."""
    org = Organization(name="Backfill Seed Org")
    db_session.add(org)
    await db_session.flush()

    await db_session.execute(
        text(
            """
            INSERT INTO subscriptions (
              organization_id, plan_code, status, price_monthly_sar,
              current_period_start, current_period_end, trial_ends_at
            )
            SELECT
              o.id,
              'growth',
              'trialing',
              (SELECT price_monthly_sar FROM plans WHERE code = 'growth'),
              now(),
              now() + interval '1 month',
              now() + interval '14 days'
            FROM organizations o
            WHERE o.id = :org_id
              AND o.deleted_at IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM subscriptions s
                WHERE s.organization_id = o.id
                  AND s.status IN ('trialing', 'active', 'past_due')
              )
            """
        ),
        {"org_id": org.id},
    )
    await db_session.flush()

    sub = (
        await db_session.execute(
            text(
                """
                SELECT plan_code, status FROM subscriptions
                WHERE organization_id = :org_id
                  AND status IN ('trialing', 'active', 'past_due')
                """
            ),
            {"org_id": org.id},
        )
    ).one()
    assert sub.plan_code == "growth"
    assert sub.status == "trialing"
    # ORM visibility for the same row
    rows = (
        await db_session.execute(
            text("SELECT id FROM subscriptions WHERE organization_id = :org_id"),
            {"org_id": org.id},
        )
    ).fetchall()
    assert len(rows) == 1
    assert await db_session.get(Subscription, rows[0].id) is not None
