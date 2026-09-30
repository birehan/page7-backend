"""Publishing HTTP routes — publish/retry under posts prefix; queue at brand scope."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import AccessContext, require_capability, require_membership
from app.db.session import get_db_session
from app.features.posts import service as posts
from app.features.posts.schemas import PostOut
from app.features.publishing import http as publishing_http
from app.features.publishing.schemas import PublishingQueueOut
from app.integrations.storage import get_object_storage
from app.integrations.storage.ports import ObjectStorage

posts_actions_router = APIRouter(
    prefix="/orgs/{orgId}/brands/{brandId}/posts", tags=["posts"]
)
brand_router = APIRouter(
    prefix="/orgs/{orgId}/brands/{brandId}", tags=["publishing"]
)


def _actor(ctx: AccessContext) -> posts.Actor:
    return posts.user_actor(user_id=ctx.user_id, name=ctx.user_name)


@posts_actions_router.post(
    "/{postId}/publish",
    response_model=PostOut,
    response_model_exclude_none=True,
)
async def publish_now(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.publishNow"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PostOut:
    return await publishing_http.publish_now(  # type: ignore[no-any-return]
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        actor=_actor(ctx),
        storage=storage,
        idempotency_key=idempotency_key,
    )


@posts_actions_router.post(
    "/{postId}/retry-publish",
    response_model=PostOut,
    response_model_exclude_none=True,
)
async def retry_publish(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.publishNow"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> PostOut:
    return await publishing_http.retry_publish(  # type: ignore[no-any-return]
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        actor=_actor(ctx),
        storage=storage,
        idempotency_key=idempotency_key,
    )


@brand_router.get(
    "/publishing-queue",
    response_model=PublishingQueueOut,
    response_model_exclude_none=True,
)
async def publishing_queue(
    brandId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> PublishingQueueOut:
    return await publishing_http.get_publishing_queue(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        storage=storage,
    )
