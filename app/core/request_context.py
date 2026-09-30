from __future__ import annotations

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Literal

import structlog
from fastapi import Depends, Path
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.core.time import utc_now
from app.db.session import get_db_session

REQUEST_ID_HEADER = "X-Request-Id"


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request id — the inbound header if the caller sent one, else a
    fresh one — stashes it on `request.state` for handlers/error handlers, binds it
    into structlog's contextvars so every log line emitted while handling this
    request carries it, and echoes it back on the response header.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response


# --- AccessContext / capability-matrix (Phase 2) ---------------------------
#
# Added here, not a new module — this file already carries the request-id/
# correlation-id binding every request goes through; membership/role
# resolution is the natural extension once Phase 2's tables make it possible.

Role = Literal["owner", "admin", "editor", "approver", "viewer"]

Capability = Literal[
    "billing.manage",
    "brand.delete",
    "brand.edit",
    "calendar.generate",
    "org.delete",
    "post.approve",
    "post.comment",
    "post.create",
    "post.edit",
    "post.publishNow",
    "post.reject",
    "post.requestChanges",
    "post.schedule",
    "post.submit",
    "post.unschedule",
    "publishing.freeze",
    "review_link.create",
    "settings.manage",
]

# Transcribed verbatim (alphabetically sorted per role) from
# docs/contracts/capabilities.json, itself generated from the frontend's own
# fixture (pgblank-web/src/shared/lib/permissions.ts). The byte-for-byte
# parity test (tests/unit/test_capability_matrix.py) is the only mechanism
# that keeps this in sync — nothing else notices the two silently diverging.
CAPABILITY_MATRIX: dict[Role, list[Capability]] = {
    "viewer": [],
    "approver": [
        "post.approve",
        "post.comment",
        "post.reject",
        "post.requestChanges",
        "review_link.create",
    ],
    "editor": [
        "brand.edit",
        "calendar.generate",
        "post.comment",
        "post.create",
        "post.edit",
        "post.submit",
        "review_link.create",
    ],
    "admin": [
        "billing.manage",
        "brand.delete",
        "brand.edit",
        "calendar.generate",
        "post.approve",
        "post.comment",
        "post.create",
        "post.edit",
        "post.publishNow",
        "post.reject",
        "post.requestChanges",
        "post.schedule",
        "post.submit",
        "post.unschedule",
        "publishing.freeze",
        "review_link.create",
        "settings.manage",
    ],
    "owner": [
        "billing.manage",
        "brand.delete",
        "brand.edit",
        "calendar.generate",
        "org.delete",
        "post.approve",
        "post.comment",
        "post.create",
        "post.edit",
        "post.publishNow",
        "post.reject",
        "post.requestChanges",
        "post.schedule",
        "post.submit",
        "post.unschedule",
        "publishing.freeze",
        "review_link.create",
        "settings.manage",
    ],
}

# Meaningful only across the unambiguous admin/owner tier. `editor` and
# `approver` are a deliberate disjoint pair (architecture/05 §3's
# "capability-not-hierarchy design"), not two points on one scale — both rank
# equally here so `require_min_role` must never be called with `"editor"` or
# `"approver"` as the threshold; use `require_capability` for anything below
# the admin tier.
_ROLE_RANK: dict[Role, int] = {"viewer": 0, "editor": 1, "approver": 1, "admin": 2, "owner": 3}


@dataclass(frozen=True)
class AuthenticatedUser:
    """`organization_id` is the session's own active org (`sessions.
    organization_id` — "the active org; sessionPayload.organizationId",
    architecture/02 §1), not a claim from the path. `GET /auth/me` has no
    `:orgId` path param at all, so this is its only source for that field;
    an org-scoped endpoint still resolves its own `AccessContext` separately
    via `get_access_context`, which re-validates against `memberships`.
    """

    user_id: uuid.UUID
    organization_id: uuid.UUID
    session_id: uuid.UUID
    expires_at: datetime


@dataclass(frozen=True)
class AccessContext:
    """Resolved once per request from the session cookie and the caller's own
    `memberships` row for the org in the path — never from an unauthenticated
    claim in the path itself (architecture/05 §3).
    """

    user_id: uuid.UUID
    organization_id: uuid.UUID
    role: Role
    user_name: str


async def require_session(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AuthenticatedUser:
    raw = request.cookies.get(settings.auth.session_cookie_name)
    if raw is None:
        raise ApiError("UNAUTHORIZED", "Not authenticated", status_code=401)
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    row = (
        await db.execute(
            text(
                "SELECT id, user_id, organization_id, expires_at FROM sessions "
                "WHERE token_hash = :token_hash AND revoked_at IS NULL"
            ),
            {"token_hash": token_hash},
        )
    ).first()
    if row is None or row.expires_at < utc_now():
        raise ApiError("UNAUTHORIZED", "Not authenticated", status_code=401)
    return AuthenticatedUser(
        user_id=row.user_id,
        organization_id=row.organization_id,
        session_id=row.id,
        expires_at=row.expires_at,
    )


async def get_access_context(
    org_id: Annotated[uuid.UUID, Path(alias="orgId")],
    user: Annotated[AuthenticatedUser, Depends(require_session)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> AccessContext:
    """architecture/05 §3, transcribed directly: `organization_id` is
    validated against a real `memberships` row, never trusted from the path.
    Zero rows is `404`, never `403` — collapsing "does not exist" and "exists
    outside your tenant" into one response removes the membership-sweep
    oracle a `403` would otherwise hand an attacker or a departed member.

    Joins `users` for its own `name` too — a small, minimal extension of the
    same justified raw-SQL pattern (`core/` cannot import a feature's
    `models.py`), so callers needing "who did this" for an audit row or a
    denormalized display name (e.g. `org_settings.frozen_by_name`) don't each
    need their own cross-feature workaround.
    """
    row = (
        await db.execute(
            text(
                "SELECT memberships.role, users.name FROM memberships "
                "JOIN users ON users.id = memberships.user_id "
                "WHERE memberships.organization_id = :org_id "
                "AND memberships.user_id = :user_id"
            ),
            {"org_id": org_id, "user_id": user.user_id},
        )
    ).first()
    if row is None:
        raise ApiError("NOT_FOUND", "Not found", status_code=404)
    return AccessContext(
        user_id=user.user_id, organization_id=org_id, role=row[0], user_name=row[1]
    )


# Endpoints that need nothing beyond "the caller is a member of this org" (no
# specific capability or minimum role) depend on this directly.
require_membership = get_access_context


async def get_session_access_context(
    user: Annotated[AuthenticatedUser, Depends(require_session)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> AccessContext:
    """Tenancy for routes that do not carry `:orgId` in the path (e.g. `/v1/ai/*`).

    Resolves the org from the session's active organization and re-validates it
    against `memberships` — same 404-not-403 discipline as `get_access_context`.
    """
    row = (
        await db.execute(
            text(
                "SELECT memberships.role, users.name FROM memberships "
                "JOIN users ON users.id = memberships.user_id "
                "WHERE memberships.organization_id = :org_id "
                "AND memberships.user_id = :user_id"
            ),
            {"org_id": user.organization_id, "user_id": user.user_id},
        )
    ).first()
    if row is None:
        raise ApiError("NOT_FOUND", "Not found", status_code=404)
    return AccessContext(
        user_id=user.user_id,
        organization_id=user.organization_id,
        role=row[0],
        user_name=row[1],
    )


require_session_membership = get_session_access_context


def require_capability(
    capability: Capability,
) -> Callable[[AccessContext], Awaitable[AccessContext]]:
    async def _dependency(
        ctx: Annotated[AccessContext, Depends(get_access_context)],
    ) -> AccessContext:
        if capability not in CAPABILITY_MATRIX[ctx.role]:
            raise ApiError("FORBIDDEN", "Missing required capability", status_code=403)
        return ctx

    return _dependency


def require_session_capability(
    capability: Capability,
) -> Callable[[AccessContext], Awaitable[AccessContext]]:
    """Capability check for session-scoped (no `:orgId`) routes."""

    async def _dependency(
        ctx: Annotated[AccessContext, Depends(get_session_access_context)],
    ) -> AccessContext:
        if capability not in CAPABILITY_MATRIX[ctx.role]:
            raise ApiError("FORBIDDEN", "Missing required capability", status_code=403)
        return ctx

    return _dependency


def require_min_role(min_role: Role) -> Callable[[AccessContext], Awaitable[AccessContext]]:
    async def _dependency(
        ctx: Annotated[AccessContext, Depends(get_access_context)],
    ) -> AccessContext:
        if _ROLE_RANK[ctx.role] < _ROLE_RANK[min_role]:
            raise ApiError("FORBIDDEN", "Insufficient role", status_code=403)
        return ctx

    return _dependency
