from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.features import audit, brands
from app.features.strategy import repository
from app.features.strategy.models import Strategy
from app.features.strategy.schemas import StrategyOut, UpdateStrategyBody


def _to_out(row: Strategy) -> StrategyOut:
    return StrategyOut.model_validate(
        {
            "id": row.id,
            "goals": row.goals,
            "cadence": row.cadence,
            "updatedAt": row.updated_at,
        }
    )


async def _assert_brand_exists(
    session: AsyncSession, *, organization_id: uuid.UUID, brand_id: uuid.UUID
) -> None:
    brand = await brands.get_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )
    if brand is None:
        raise ApiError("NOT_FOUND", "Brand not found", status_code=404)


async def get_or_create_strategy(
    session: AsyncSession, *, organization_id: uuid.UUID, brand_id: uuid.UUID
) -> StrategyOut:
    await _assert_brand_exists(
        session, organization_id=organization_id, brand_id=brand_id
    )
    row = await repository.get_strategy(
        session, organization_id=organization_id, brand_id=brand_id
    )
    if row is None:
        row = await repository.insert_strategy(
            session, organization_id=organization_id, brand_id=brand_id
        )
    return _to_out(row)


async def update_strategy(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    body: UpdateStrategyBody,
    actor_user_id: uuid.UUID,
    actor_name: str,
) -> StrategyOut:
    """Last-write-wins — the wire contract carries no strategy version field."""
    await _assert_brand_exists(
        session, organization_id=organization_id, brand_id=brand_id
    )
    row = await repository.get_strategy(
        session, organization_id=organization_id, brand_id=brand_id
    )
    if row is None:
        row = await repository.insert_strategy(
            session, organization_id=organization_id, brand_id=brand_id
        )

    if body.goals is not None:
        row.goals = [g.model_dump(mode="json", by_alias=True) for g in body.goals]
    if body.cadence is not None:
        row.cadence = dict(body.cadence)
    row.version = row.version + 1
    await session.flush()
    await session.refresh(row)

    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="strategy.updated",
        target_type="strategy",
        target_id=row.id,
    )
    return _to_out(row)
