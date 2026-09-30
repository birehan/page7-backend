from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import AccessContext, require_capability, require_membership
from app.db.session import get_db_session
from app.features.strategy import service as strategy
from app.features.strategy.schemas import StrategyOut, UpdateStrategyBody

router = APIRouter(
    prefix="/orgs/{orgId}/brands/{brandId}/strategy", tags=["strategy"]
)


@router.get("", response_model=StrategyOut, response_model_exclude_none=True)
async def get_strategy(
    brandId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> StrategyOut:
    return await strategy.get_or_create_strategy(
        db, organization_id=ctx.organization_id, brand_id=brandId
    )


@router.patch("", response_model=StrategyOut, response_model_exclude_none=True)
async def update_strategy(
    brandId: uuid.UUID,  # noqa: N803
    body: UpdateStrategyBody,
    ctx: Annotated[AccessContext, Depends(require_capability("brand.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> StrategyOut:
    return await strategy.update_strategy(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        body=body,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
    )
