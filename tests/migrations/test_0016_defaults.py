"""Server-default smoke for 0016 billing tables."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.billing.models import Invoice, InvoiceCounter, Plan, Subscription
from app.features.organizations.models import Organization


@pytest.mark.asyncio
async def test_0016_plans_seeded(db_session: AsyncSession) -> None:
    starter = await db_session.get(Plan, "starter")
    growth = await db_session.get(Plan, "growth")
    agency = await db_session.get(Plan, "agency")
    assert starter is not None
    assert growth is not None
    assert agency is not None
    assert starter.price_monthly_sar == Decimal("599.00")
    assert growth.brands_limit == 3
    assert agency.ai_generations_monthly == 3000


@pytest.mark.asyncio
async def test_0016_invoice_counter_seeded(db_session: AsyncSession) -> None:
    row = await db_session.get(InvoiceCounter, "pgblank")
    assert row is not None
    assert row.last_number >= 0


@pytest.mark.asyncio
async def test_0016_invoice_total_check(db_session: AsyncSession) -> None:
    org = Organization(name="Invoice Check Org")
    db_session.add(org)
    await db_session.flush()
    now = datetime.now(UTC)
    sub = Subscription(
        organization_id=org.id,
        plan_code="growth",
        status="active",
        price_monthly_sar=Decimal("1499.00"),
        current_period_start=now,
        current_period_end=now + timedelta(days=30),
    )
    db_session.add(sub)
    await db_session.flush()

    good = Invoice(
        organization_id=org.id,
        subscription_id=sub.id,
        number=f"INV-TEST-{org.id.hex[:8]}",
        status="pending",
        amount_subtotal=Decimal("100.00"),
        vat_rate=Decimal("0.1500"),
        vat_amount=Decimal("15.00"),
        total=Decimal("115.00"),
        seller_vat_number="300000000000003",
        period_start=now.date(),
        period_end=(now + timedelta(days=30)).date(),
        issued_at=now,
    )
    db_session.add(good)
    await db_session.flush()

    bad = Invoice(
        organization_id=org.id,
        subscription_id=sub.id,
        number=f"INV-BAD-{org.id.hex[:8]}",
        status="pending",
        amount_subtotal=Decimal("100.00"),
        vat_rate=Decimal("0.1500"),
        vat_amount=Decimal("15.00"),
        total=Decimal("999.00"),
        seller_vat_number="300000000000003",
        period_start=now.date(),
        period_end=(now + timedelta(days=30)).date(),
        issued_at=now,
    )
    db_session.add(bad)
    with pytest.raises(IntegrityError):
        await db_session.flush()
