from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import AccessContext, require_capability, require_membership
from app.db.session import get_db_session
from app.features.cultural_events import service as cultural_events
from app.features.cultural_events.schemas import CulturalEventOut

router = APIRouter(prefix="/orgs/{orgId}/cultural-events", tags=["culturalCalendar"])


@router.get("", response_model=list[CulturalEventOut], response_model_exclude_none=True)
async def list_cultural_events(
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[CulturalEventOut]:
    return await cultural_events.list_cultural_events(
        db, organization_id=ctx.organization_id
    )


@router.post(
    "/{eventId}/toggle",
    response_model=CulturalEventOut,
    response_model_exclude_none=True,
)
async def toggle_cultural_event(
    eventId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("brand.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> CulturalEventOut:
    return await cultural_events.toggle_cultural_event(
        db,
        organization_id=ctx.organization_id,
        event_id=eventId,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
    )
