from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.strategy.models import Strategy


async def get_strategy(
    session: AsyncSession, *, organization_id: uuid.UUID, brand_id: uuid.UUID
) -> Strategy | None:
    stmt = select(Strategy).where(
        Strategy.brand_id == brand_id, Strategy.organization_id == organization_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def insert_strategy(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    goals: list[Any] | None = None,
    cadence: dict[str, Any] | None = None,
) -> Strategy:
    row = Strategy(
        organization_id=organization_id,
        brand_id=brand_id,
        goals=goals if goals is not None else [],
        cadence=cadence if cadence is not None else {},
        version=1,
    )
    session.add(row)
    await session.flush()
    return row
