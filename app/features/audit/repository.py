from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import decode_cursor, encode_cursor
from app.features.audit.models import AuditLog

DEFAULT_PAGE_SIZE = 20


async def insert(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID | None,
    actor_kind: str,
    actor_user_id: uuid.UUID | None,
    actor_ref: str,
    actor_name: str,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    meta: dict[str, Any] | None,
) -> AuditLog:
    log = AuditLog(
        organization_id=organization_id,
        brand_id=brand_id,
        actor_kind=actor_kind,
        actor_user_id=actor_user_id,
        actor_ref=actor_ref,
        actor_name=actor_name,
        action=action,
        target_type=target_type,
        target_id=target_id,
        meta=meta,
    )
    session.add(log)
    await session.flush()
    return log


async def list_page(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    cursor: str | None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> tuple[Sequence[AuditLog], str | None]:
    stmt = select(AuditLog).where(AuditLog.organization_id == organization_id)
    if cursor is not None:
        parsed = decode_cursor(cursor)
        cursor_created_at = datetime.fromisoformat(parsed["created_at"])
        cursor_id = uuid.UUID(parsed["id"])
        stmt = stmt.where(tuple_(AuditLog.created_at, AuditLog.id) < (cursor_created_at, cursor_id))
    stmt = stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit + 1)

    rows = (await session.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    page = rows[:limit]

    next_cursor = None
    if has_more:
        last = page[-1]
        next_cursor = encode_cursor({"created_at": last.created_at.isoformat(), "id": str(last.id)})
    return page, next_cursor
