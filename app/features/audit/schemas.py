from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from app.core.pagination import Page
from app.core.schema import CamelModel


class AuditLogEntry(CamelModel):
    """Matches `auditLogEntrySchema`'s `kind: 'audit'` arm in
    `pgblank-web/src/shared/api/contracts/audit.contract.ts` field for field.
    `actorId` is `AuditLog.actor_ref` — a display id, not necessarily
    `actor_user_id` (a system or guest actor has no user row at all).
    """

    kind: Literal["audit"] = "audit"
    id: uuid.UUID
    organization_id: uuid.UUID
    actor_id: str
    actor_name: str
    action: str
    target_type: str
    target_id: uuid.UUID
    created_at: datetime
    meta: dict[str, Any] | None = None


AuditLogPage = Page[AuditLogEntry]
