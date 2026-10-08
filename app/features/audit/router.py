from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import AccessContext, require_min_role
from app.db.session import get_db_session
from app.features.audit import service
from app.features.audit.models import AuditLog
from app.features.audit.schemas import AuditLogEntry, AuditLogPage

router = APIRouter(prefix="/orgs/{orgId}", tags=["audit"])


def _to_entry(log: AuditLog) -> AuditLogEntry:
    return AuditLogEntry(
        id=log.id,
        organization_id=log.organization_id,
        actor_id=log.actor_ref,
        actor_name=log.actor_name,
        action=log.action,
        target_type=log.target_type,
        target_id=log.target_id,
        created_at=log.created_at,
        meta=log.meta,
    )


# `exclude_none`: the frontend's `meta` field is optional (key may be absent),
# not nullable (`z.record(...).optional()`, not `.nullable()`) — the wire
# schema has no `"null"` variant for it, so an explicit `"meta": null` fails
# contract validation. `AuditLogEntry.meta` and `Page.next_cursor` are the
# only two fields this can ever affect on this response.
@router.get("/audit", response_model=AuditLogPage, response_model_exclude_none=True)
async def list_audit_log(
    ctx: Annotated[AccessContext, Depends(require_min_role("admin"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    cursor: str | None = Query(default=None),
) -> AuditLogPage:
    """Admins and owners only: the log names every member's actions. Audit rows only for
    now; the `ai_decisions` half of the feed is not implemented.
    """
    logs, next_cursor = await service.list_page(
        db, organization_id=ctx.organization_id, cursor=cursor
    )
    return AuditLogPage(items=[_to_entry(log) for log in logs], next_cursor=next_cursor)
