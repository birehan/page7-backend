"""FastAPI dependency for the `Idempotency-Key` header (architecture/04).

First wired in Phase 4 for `POST .../brands/:brandId/generate`. Replays a stored
response on a matching key+body; raises 422 IDEMPOTENCY_MISMATCH / 409 IN_PROGRESS
on the documented edge cases.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.request_context import AccessContext, require_membership
from app.db.session import get_db_session
from app.infrastructure.idempotency.store import (
    IdempotencyInProgressError,
    IdempotencyMismatchError,
    IdempotencyStore,
    hash_request_body,
)


@dataclass
class IdempotencyContext:
    key: str
    scope: str
    organization_id: uuid.UUID
    is_replay: bool
    response_status: int | None
    response_body: dict[str, Any] | None
    store: IdempotencyStore

    async def complete(self, *, status_code: int, body: dict[str, Any]) -> None:
        if self.is_replay:
            return
        await self.store.complete(
            organization_id=self.organization_id,
            key=self.key,
            status_code=status_code,
            body=body,
        )


def require_idempotency(
    scope: str,
) -> Callable[..., Awaitable[IdempotencyContext]]:
    async def _dependency(
        request: Request,
        ctx: Annotated[AccessContext, Depends(require_membership)],
        db: Annotated[AsyncSession, Depends(get_db_session)],
        idempotency_key: Annotated[
            str | None, Header(alias="Idempotency-Key")
        ] = None,
    ) -> IdempotencyContext:
        if not idempotency_key:
            raise ApiError(
                "VALIDATION",
                "Idempotency-Key header is required",
                status_code=422,
            )
        body_bytes = await request.body()
        request_hash = hash_request_body(body_bytes)
        store = IdempotencyStore(db)
        try:
            result = await store.begin(
                organization_id=ctx.organization_id,
                key=idempotency_key,
                scope=scope,
                request_hash=request_hash,
            )
        except IdempotencyMismatchError as exc:
            raise ApiError(
                "IDEMPOTENCY_MISMATCH",
                "Idempotency-Key reused with a different request body",
                status_code=422,
            ) from exc
        except IdempotencyInProgressError as exc:
            raise ApiError(
                "IN_PROGRESS",
                "A request with this Idempotency-Key is still in progress",
                status_code=409,
            ) from exc

        return IdempotencyContext(
            key=idempotency_key,
            scope=scope,
            organization_id=ctx.organization_id,
            is_replay=result.is_replay,
            response_status=result.response_status,
            response_body=result.response_body,
            store=store,
        )

    return _dependency


def replay_response(idem: IdempotencyContext, response: Response) -> dict[str, Any]:
    """Return a previously stored JSON body and status for a replayed key."""
    assert idem.response_status is not None and idem.response_body is not None
    response.status_code = idem.response_status
    return idem.response_body


def serialize_model(model: Any) -> dict[str, Any]:
    """CamelCase JSON-compatible dict for storing in idempotency_keys.response_body."""
    payload: dict[str, Any] = json.loads(model.model_dump_json(by_alias=True))
    return payload
