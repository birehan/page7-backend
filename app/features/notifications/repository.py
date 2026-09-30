from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import decode_cursor, encode_cursor
from app.core.time import utc_now
from app.features.notifications.models import Notification, NotificationRecipient

DEFAULT_PAGE_SIZE = 20


async def list_page(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    cursor: str | None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> tuple[Sequence[tuple[Notification, datetime | None]], str | None]:
    stmt = (
        select(Notification, NotificationRecipient.read_at)
        .join(
            NotificationRecipient,
            (NotificationRecipient.notification_id == Notification.id)
            & (NotificationRecipient.user_id == user_id),
        )
        .where(Notification.organization_id == organization_id)
    )
    if cursor is not None:
        parsed = decode_cursor(cursor)
        cursor_created_at = datetime.fromisoformat(parsed["created_at"])
        cursor_id = uuid.UUID(parsed["id"])
        stmt = stmt.where(
            tuple_(Notification.created_at, Notification.id) < (cursor_created_at, cursor_id)
        )
    stmt = stmt.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit + 1)

    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > limit
    page = [(row[0], row[1]) for row in rows[:limit]]

    next_cursor = None
    if has_more:
        last_notification = page[-1][0]
        next_cursor = encode_cursor(
            {
                "created_at": last_notification.created_at.isoformat(),
                "id": str(last_notification.id),
            }
        )
    return page, next_cursor


async def mark_read(
    session: AsyncSession, *, notification_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[Notification, datetime | None] | None:
    now = utc_now()
    stmt = (
        update(NotificationRecipient)
        .where(
            NotificationRecipient.notification_id == notification_id,
            NotificationRecipient.user_id == user_id,
        )
        .values(read_at=now)
        .returning(NotificationRecipient.read_at)
    )
    result = (await session.execute(stmt)).first()
    if result is None:
        return None
    notification = await session.get(Notification, notification_id)
    if notification is None:
        return None
    return notification, result[0]


async def mark_all_read(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> Sequence[tuple[Notification, datetime | None]]:
    now = utc_now()
    stmt = (
        update(NotificationRecipient)
        .where(
            NotificationRecipient.user_id == user_id,
            NotificationRecipient.read_at.is_(None),
            NotificationRecipient.notification_id.in_(
                select(Notification.id).where(Notification.organization_id == organization_id)
            ),
        )
        .values(read_at=now)
        .returning(NotificationRecipient.notification_id)
    )
    updated_ids = [row[0] for row in (await session.execute(stmt)).all()]
    if not updated_ids:
        return []
    rows = (
        (await session.execute(select(Notification).where(Notification.id.in_(updated_ids))))
        .scalars()
        .all()
    )
    return [(row, now) for row in rows]


async def notify(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID | None,
    type: str,
    message_key: str,
    params: dict[str, object],
    target_href: str,
    actor_user_id: uuid.UUID | None,
    actor_ref: str | None,
    recipient_user_ids: Sequence[uuid.UUID],
) -> Notification:
    """No caller until a later phase's first real event (Phase 6's
    `post.submit` firing `approval_requested`) — built now, with no caller,
    same pattern as `rate_limits`/`idempotency_keys` in Phase 1.
    """
    notification = Notification(
        organization_id=organization_id,
        brand_id=brand_id,
        type=type,
        message_key=message_key,
        params=params,
        target_href=target_href,
        actor_user_id=actor_user_id,
        actor_ref=actor_ref,
    )
    session.add(notification)
    await session.flush()
    for user_id in recipient_user_ids:
        session.add(NotificationRecipient(notification_id=notification.id, user_id=user_id))
    await session.flush()
    return notification
