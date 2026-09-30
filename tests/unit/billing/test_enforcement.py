"""Unit tests for brand-limit and AI-credit comparison helpers."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.features.billing import repository
from app.features.billing.models import Plan, Subscription
from app.features.billing.service import enforce_ai_credit_limit, enforce_brand_limit
from app.features.brands.models import Brand
from app.features.content_ai.models import AiUsageCounter
from app.features.organizations.models import Organization, OrgSettings


async def _seed_org_with_plan(
    session: AsyncSession,
    *,
    plan_code: str = "starter",
    brands_limit: int = 1,
    ai_limit: int = 200,
) -> uuid.UUID:
    # Ensure plan row exists (migration seed may already have it).
    existing = await session.get(Plan, plan_code)
    if existing is None:
        session.add(
            Plan(
                code=plan_code,
                name=plan_code.title(),
                price_monthly_sar=Decimal("599.00"),
                ai_generations_monthly=ai_limit,
                brands_limit=brands_limit,
                seats_limit=5,
                channels_limit=2,
            )
        )
        await session.flush()
    else:
        existing.brands_limit = brands_limit
        existing.ai_generations_monthly = ai_limit
        await session.flush()

    org = Organization(name=f"Limit Org {uuid.uuid4().hex[:6]}")
    session.add(org)
    await session.flush()
    session.add(OrgSettings(organization_id=org.id))
    now = datetime.now(UTC)
    session.add(
        Subscription(
            organization_id=org.id,
            plan_code=plan_code,
            status="active",
            price_monthly_sar=Decimal("599.00"),
            current_period_start=now,
            current_period_end=now + timedelta(days=30),
        )
    )
    await session.flush()
    return org.id


@pytest.mark.asyncio
async def test_enforce_brand_limit_blocks_at_cap(db_session: AsyncSession) -> None:
    org_id = await _seed_org_with_plan(db_session, brands_limit=1)
    db_session.add(
        Brand(
            organization_id=org_id,
            name="Only Brand",
            industry="retail",
            city="Riyadh",
        )
    )
    await db_session.flush()

    with pytest.raises(ApiError) as exc:
        await enforce_brand_limit(db_session, organization_id=org_id)
    assert exc.value.code == "PLAN_LIMIT_BRANDS"
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_enforce_brand_limit_allows_under_cap(db_session: AsyncSession) -> None:
    org_id = await _seed_org_with_plan(db_session, brands_limit=3)
    await enforce_brand_limit(db_session, organization_id=org_id)


@pytest.mark.asyncio
async def test_enforce_ai_credit_limit_blocks_at_cap(db_session: AsyncSession) -> None:
    org_id = await _seed_org_with_plan(db_session, ai_limit=10)
    period = repository.period_month_of(datetime.now(UTC))
    db_session.add(
        AiUsageCounter(
            organization_id=org_id,
            period_month=period,
            generations=10,
        )
    )
    await db_session.flush()

    with pytest.raises(ApiError) as exc:
        await enforce_ai_credit_limit(db_session, organization_id=org_id)
    assert exc.value.code == "AI_CREDITS_EXHAUSTED"
    assert exc.value.status_code == 429
    assert exc.value.details["limit"] == 10
    assert exc.value.details["used"] == 10


@pytest.mark.asyncio
async def test_enforce_ai_credit_limit_allows_under_cap(
    db_session: AsyncSession,
) -> None:
    org_id = await _seed_org_with_plan(db_session, ai_limit=200)
    await enforce_ai_credit_limit(db_session, organization_id=org_id)
