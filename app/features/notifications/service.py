from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.features import organizations as orgs_feature
from app.features import team as team_feature
from app.features.notifications import repository
from app.features.notifications.models import Notification

# Role filters for fan-out when recipients are not supplied explicitly
# (architecture/02 §10).
_TYPE_ROLES: dict[str, frozenset[str]] = {
    "approval_requested": frozenset({"approver", "admin", "owner"}),
    "ai_credits_warning": frozenset({"admin", "owner"}),
}

# Frontend org_settings.notification_prefs keys that gate a notification type.
# Types without a mapping default to enabled when the key is absent.
_TYPE_PREF_KEYS: dict[str, str] = {
    "post_failed": "publishFailure",
    "channel_expiring": "channelExpiry",
    "channel_disconnected": "channelExpiry",
    "weekly_insight": "weeklyReport",
}


def _pref_enabled(prefs: dict[str, Any], notif_type: str) -> bool:
    """Empty prefs → all enabled. Explicit false for a type (or its mapped
    frontend key) suppresses the fan-out entirely.
    """
    if not prefs:
        return True
    if notif_type in prefs:
        return bool(prefs[notif_type])
    mapped = _TYPE_PREF_KEYS.get(notif_type)
    if mapped is not None and mapped in prefs:
        return bool(prefs[mapped])
    return True


async def fan_out(
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
    recipient_user_ids: Sequence[uuid.UUID] | None = None,
    exclude_user_ids: Sequence[uuid.UUID] = (),
) -> Notification | None:
    """Insert a notification and fan out to recipients filtered by role and
    org_settings.notification_prefs. Returns None when no recipients remain.
    """
    settings = await orgs_feature.get_org_settings(session, organization_id)
    prefs = dict(settings.notification_prefs or {})
    if not _pref_enabled(prefs, type):
        return None

    exclude = set(exclude_user_ids)
    if recipient_user_ids is not None:
        recipients = [uid for uid in recipient_user_ids if uid not in exclude]
    else:
        roles = _TYPE_ROLES.get(type)
        memberships = await team_feature.list_memberships(
            session, organization_id=organization_id
        )
        recipients = []
        for membership in memberships:
            if membership.user_id in exclude:
                continue
            if roles is not None and membership.role not in roles:
                continue
            recipients.append(membership.user_id)

    seen: set[uuid.UUID] = set()
    unique: list[uuid.UUID] = []
    for uid in recipients:
        if uid not in seen:
            seen.add(uid)
            unique.append(uid)

    if not unique:
        return None

    return await repository.notify(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        type=type,
        message_key=message_key,
        params=params,
        target_href=target_href,
        actor_user_id=actor_user_id,
        actor_ref=actor_ref,
        recipient_user_ids=unique,
    )


async def list_page(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID, cursor: str | None
) -> tuple[Sequence[tuple[Notification, datetime | None]], str | None]:
    return await repository.list_page(
        session, organization_id=organization_id, user_id=user_id, cursor=cursor
    )


async def mark_read(
    session: AsyncSession, *, notification_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[Notification, datetime | None]:
    result = await repository.mark_read(session, notification_id=notification_id, user_id=user_id)
    if result is None:
        raise ApiError("NOT_FOUND", "Notification not found", status_code=404)
    return result


async def mark_all_read(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> Sequence[tuple[Notification, datetime | None]]:
    return await repository.mark_all_read(session, organization_id=organization_id, user_id=user_id)
