from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.infrastructure.idempotency.models import IdempotencyKey

EXPIRY = timedelta(hours=24)


class IdempotencyMismatchError(Exception):
    """Same key, different request body — architecture/04 says this is 422
    IDEMPOTENCY_MISMATCH, never a silent replay of the stored response.
    """


class IdempotencyInProgressError(Exception):
    """Same key, no stored response yet — the original request is still in
    flight. Callers surface this as 409 IN_PROGRESS with a Retry-After header.
    """


@dataclass(frozen=True)
class IdempotencyResult:
    is_replay: bool
    response_status: int | None
    response_body: dict[str, Any] | None


def hash_request_body(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


class IdempotencyStore:
    """No caller until Phase 6 wires the `Idempotency-Key` header dependency into
    mutating endpoints. Built now so that phase only has to wire it up, not design
    the storage semantics from scratch.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def begin(
        self, *, organization_id: uuid.UUID, key: str, scope: str, request_hash: str
    ) -> IdempotencyResult:
        """Look up an existing key, or claim a fresh one. Raises
        `IdempotencyMismatchError` on a body mismatch and
        `IdempotencyInProgressError` if the original request hasn't finished yet.
        """
        existing = await self._session.get(IdempotencyKey, (organization_id, key))
        if existing is not None:
            if existing.request_hash != request_hash:
                raise IdempotencyMismatchError
            if existing.response_status is None:
                raise IdempotencyInProgressError
            return IdempotencyResult(True, existing.response_status, existing.response_body)

        stmt = (
            insert(IdempotencyKey)
            .values(
                organization_id=organization_id,
                key=key,
                created_at=utc_now(),
                scope=scope,
                request_hash=request_hash,
                expires_at=utc_now() + EXPIRY,
            )
            .on_conflict_do_nothing(index_elements=["organization_id", "key"])
        )
        await self._session.execute(stmt)
        return IdempotencyResult(False, None, None)

    async def complete(
        self, *, organization_id: uuid.UUID, key: str, status_code: int, body: dict[str, Any]
    ) -> None:
        row = await self._session.get(IdempotencyKey, (organization_id, key))
        if row is None:
            return
        row.response_status = status_code
        row.response_body = body

    async def purge_expired(self) -> int:
        """Called from a maintenance job (Phase 3), not from the request path."""
        result = await self._session.execute(
            select(IdempotencyKey).where(IdempotencyKey.expires_at < utc_now())
        )
        rows = result.scalars().all()
        for row in rows:
            await self._session.delete(row)
        return len(rows)
