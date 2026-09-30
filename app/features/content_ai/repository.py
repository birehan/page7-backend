from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.features.content_ai.models import AiDecision, AiFeedback, AiUsageCounter


async def insert_decision(session: AsyncSession, **kwargs: object) -> AiDecision:
    """Insert one ai_decisions row and upsert usage counters in the same transaction."""
    decision = AiDecision(**kwargs)
    session.add(decision)
    await session.flush()
    await upsert_usage_counter(
        session,
        organization_id=decision.organization_id,
        period_month=_period_month(utc_now()),
        generations=1,
        prompt_tokens=decision.prompt_tokens or 0,
        completion_tokens=decision.completion_tokens or 0,
        cost_usd=decision.cost_usd or Decimal("0"),
    )
    return decision


def _period_month(now: datetime) -> date:
    return date(now.year, now.month, 1)


async def upsert_usage_counter(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    period_month: date,
    generations: int = 1,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: Decimal = Decimal("0"),
) -> None:
    stmt = (
        insert(AiUsageCounter)
        .values(
            organization_id=organization_id,
            period_month=period_month,
            generations=generations,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            updated_at=utc_now(),
        )
        .on_conflict_do_update(
            index_elements=["organization_id", "period_month"],
            set_={
                "generations": AiUsageCounter.generations + generations,
                "prompt_tokens": AiUsageCounter.prompt_tokens + prompt_tokens,
                "completion_tokens": AiUsageCounter.completion_tokens + completion_tokens,
                "cost_usd": AiUsageCounter.cost_usd + cost_usd,
                "updated_at": utc_now(),
            },
        )
    )
    await session.execute(stmt)
    await session.flush()


async def upsert_feedback(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    decision_id: uuid.UUID,
    user_id: uuid.UUID,
    variant_index: int,
    rating: str,
) -> AiFeedback:
    stmt = (
        insert(AiFeedback)
        .values(
            organization_id=organization_id,
            decision_id=decision_id,
            user_id=user_id,
            variant_index=variant_index,
            rating=rating,
        )
        .on_conflict_do_update(
            index_elements=["decision_id", "variant_index", "user_id"],
            set_={"rating": rating},
        )
        .returning(AiFeedback)
    )
    result = await session.execute(stmt)
    row = result.scalar_one()
    await session.flush()
    return row


async def get_decision(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    decision_id: uuid.UUID,
) -> AiDecision | None:
    result = await session.execute(
        sa.select(AiDecision).where(
            AiDecision.id == decision_id,
            AiDecision.organization_id == organization_id,
        )
    )
    return result.scalar_one_or_none()


async def find_media_id_by_r2_key(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    r2_key: str,
) -> uuid.UUID | None:
    result = await session.execute(
        sa.text(
            """
            SELECT id FROM media_assets
            WHERE organization_id = :organization_id
              AND brand_id = :brand_id
              AND r2_key = :r2_key
            LIMIT 1
            """
        ),
        {
            "organization_id": str(organization_id),
            "brand_id": str(brand_id),
            "r2_key": r2_key,
        },
    )
    row = result.first()
    return uuid.UUID(str(row[0])) if row else None


async def update_media_alt(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    r2_key: str,
    alt_ar: str,
    alt_en: str,
    ai_decision_id: uuid.UUID,
) -> uuid.UUID | None:
    result = await session.execute(
        sa.text(
            """
            UPDATE media_assets
            SET alt_ar = :alt_ar,
                alt_en = :alt_en,
                ai_decision_id = :ai_decision_id
            WHERE organization_id = :organization_id
              AND brand_id = :brand_id
              AND r2_key = :r2_key
            RETURNING id
            """
        ),
        {
            "alt_ar": alt_ar,
            "alt_en": alt_en,
            "ai_decision_id": str(ai_decision_id),
            "organization_id": str(organization_id),
            "brand_id": str(brand_id),
            "r2_key": r2_key,
        },
    )
    row = result.first()
    await session.flush()
    return uuid.UUID(str(row[0])) if row else None
