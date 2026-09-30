from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import AccessContext, require_membership
from app.db.session import get_db_session
from app.features.notifications import service
from app.features.notifications.models import Notification
from app.features.notifications.schemas import NotificationOut, NotificationPage

router = APIRouter(prefix="/orgs/{orgId}/notifications", tags=["notifications"])


def _to_out(notification: Notification, read_at: datetime | None) -> NotificationOut:
    return NotificationOut(
        id=notification.id,
        organization_id=notification.organization_id,
        type=notification.type,
        message_key=notification.message_key,
        params=notification.params,
        target_href=notification.target_href,
        actor_id=notification.actor_ref,
        read_at=read_at,
        created_at=notification.created_at,
    )


@router.get("", response_model=NotificationPage, response_model_exclude_none=True)
async def list_notifications(
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    cursor: str | None = Query(default=None),
) -> NotificationPage:
    rows, next_cursor = await service.list_page(
        db, organization_id=ctx.organization_id, user_id=ctx.user_id, cursor=cursor
    )
    return NotificationPage(
        items=[_to_out(n, read_at) for n, read_at in rows], next_cursor=next_cursor
    )


@router.post(
    "/{notificationId}/read", response_model=NotificationOut, response_model_exclude_none=True
)
async def mark_read(
    notificationId: uuid.UUID,  # noqa: N803 - matches the URL's camelCase path param
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> NotificationOut:
    notification, read_at = await service.mark_read(
        db, notification_id=notificationId, user_id=ctx.user_id
    )
    return _to_out(notification, read_at)


@router.post("/read-all", response_model=list[NotificationOut], response_model_exclude_none=True)
async def mark_all_read(
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[NotificationOut]:
    rows = await service.mark_all_read(db, organization_id=ctx.organization_id, user_id=ctx.user_id)
    return [_to_out(n, read_at) for n, read_at in rows]
