"""Server-default smoke tests for Phase 7 AI tables."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.features.auth.models import User
from app.features.brands.models import Brand, BrandVersion
from app.features.content_ai.models import AiDecision, AiFeedback, AiUsageCounter
from app.features.organizations.models import Organization


async def _seed(db_session: AsyncSession) -> tuple[Organization, Brand, User]:
    user = User(email="ai-defaults@example.com", name="AI User")
    org = Organization(name="AI Org")
    db_session.add_all([user, org])
    await db_session.flush()
    brand = Brand(
        organization_id=org.id,
        name="AI Brand",
        industry="retail",
        city="Riyadh",
        guidelines={},
    )
    db_session.add(brand)
    await db_session.flush()
    version = BrandVersion(
        organization_id=org.id,
        brand_id=brand.id,
        version=1,
        snapshot={"name": brand.name},
        reason="generated",
        author_user_id=user.id,
    )
    db_session.add(version)
    await db_session.flush()
    return org, brand, user


async def test_ai_decisions_defaults(db_session: AsyncSession) -> None:
    org, brand, user = await _seed(db_session)
    decision = AiDecision(
        organization_id=org.id,
        brand_id=brand.id,
        actor_user_id=user.id,
        kind="captions",
        provider="openai",
        model="gpt-5.5",
        prompt_version="copywriter-v5",
        inputs_hash="a" * 64,
        target_type="brand",
        target_id=brand.id,
        brand_version=1,
        status="succeeded",
    )
    db_session.add(decision)
    await db_session.flush()
    assert decision.input_summary == {}
    assert decision.output is None
    assert decision.created_at is not None
    assert decision.id is not None


async def test_ai_feedback_and_counters_defaults(db_session: AsyncSession) -> None:
    org, brand, user = await _seed(db_session)
    decision = AiDecision(
        organization_id=org.id,
        brand_id=brand.id,
        actor_user_id=user.id,
        kind="captions",
        provider="openai",
        model="gpt-5.5",
        prompt_version="copywriter-v5",
        inputs_hash="b" * 64,
        target_type="brand",
        target_id=brand.id,
        status="succeeded",
    )
    db_session.add(decision)
    await db_session.flush()

    feedback = AiFeedback(
        organization_id=org.id,
        decision_id=decision.id,
        user_id=user.id,
        variant_index=0,
        rating="up",
    )
    counter = AiUsageCounter(
        organization_id=org.id,
        period_month=date(2026, 9, 1),
    )
    db_session.add_all([feedback, counter])
    await db_session.flush()

    assert feedback.rating == "up"
    assert counter.generations == 0
    assert counter.prompt_tokens == 0
    assert counter.completion_tokens == 0
    assert counter.cost_usd == Decimal("0")
    assert counter.updated_at is not None
