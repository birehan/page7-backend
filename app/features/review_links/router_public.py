from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.db.session import get_db_session
from app.features.review_links import repository, service
from app.features.review_links.schemas import (
    ReviewDecisionBody,
    ReviewDecisionResponse,
    ReviewLinkOut,
)
from app.infrastructure.idempotency.dependency import (
    IdempotencyContext,
    replay_response,
    serialize_model,
)
from app.infrastructure.idempotency.store import (
    IdempotencyInProgressError,
    IdempotencyMismatchError,
    IdempotencyStore,
    hash_request_body,
)
from app.integrations.storage import get_object_storage
from app.integrations.storage.ports import ObjectStorage

router = APIRouter(prefix="/review", tags=["review-links-public"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get(
    "/{token}",
    response_model=ReviewLinkOut,
    response_model_exclude_none=True,
)
async def fetch_review_link(
    token: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> ReviewLinkOut:
    return await service.get_public_review_link(
        db,
        token=token,
        ip=_client_ip(request),
        storage=storage,
    )


async def _guest_idempotency(
    request: Request,
    token: str,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key")
    ] = None,
) -> IdempotencyContext:
    """Generic Idempotency-Key for unauthenticated decision — org from the link."""
    await service.rate_limit_guest(
        action="decision", token=token, ip=_client_ip(request)
    )

    if not idempotency_key:
        raise ApiError(
            "VALIDATION",
            "Idempotency-Key header is required",
            status_code=422,
        )

    link = await repository.get_by_token_hash(
        db, token_hash=service.hash_token(token)
    )
    service.assert_link_active(link)
    assert link is not None

    body_bytes = await request.body()
    request_hash = hash_request_body(body_bytes)
    store = IdempotencyStore(db)
    try:
        result = await store.begin(
            organization_id=link.organization_id,
            key=idempotency_key,
            scope="review.decision",
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
        scope="review.decision",
        organization_id=link.organization_id,
        is_replay=result.is_replay,
        response_status=result.response_status,
        response_body=result.response_body,
        store=store,
    )


@router.post(
    "/{token}/decision",
    response_model=ReviewDecisionResponse,
    response_model_exclude_none=True,
)
async def submit_review_decision(
    token: str,
    body: ReviewDecisionBody,
    request: Request,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    idem: Annotated[IdempotencyContext, Depends(_guest_idempotency)],
) -> ReviewDecisionResponse | dict[str, Any]:
    if idem.is_replay:
        return replay_response(idem, response)

    result = await service.submit_decision(
        db,
        token=token,
        body=body,
        ip=_client_ip(request),
        storage=storage,
        skip_rate_limit=True,
    )
    await idem.complete(status_code=200, body=serialize_model(result))
    return result
