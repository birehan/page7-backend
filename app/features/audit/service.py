from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.features.audit import repository
from app.features.audit.models import AuditLog

ActorKind = Literal["user", "system", "policy", "guest"]


async def record(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_kind: ActorKind,
    actor_ref: str,
    actor_name: str,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    brand_id: uuid.UUID | None = None,
    actor_user_id: uuid.UUID | None = None,
    meta: dict[str, Any] | None = None,
) -> AuditLog:
    """The one function every later feature's `service.py` calls to record a
    state change (phase-02 doc, "Backend components"). No transaction
    management here — the caller's own transaction is what makes an audit row
    atomic with the action it records; `record` only ever flushes.
    """
    return await repository.insert(
        session,
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


async def list_page(
    session: AsyncSession, *, organization_id: uuid.UUID, cursor: str | None
) -> tuple[Sequence[AuditLog], str | None]:
    return await repository.list_page(session, organization_id=organization_id, cursor=cursor)
