from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.request_context import AccessContext, require_capability, require_membership
from app.db.session import get_db_session
from app.features.posts import service as posts
from app.features.posts.schemas import (
    AddCommentBody,
    BulkApproveBody,
    BulkApproveItem,
    CreatePostBody,
    PostCommentOut,
    PostOut,
    PostVersionOut,
    ReasonBody,
    RescheduleBody,
    RescheduleResponse,
    UpdatePostBody,
)
from app.integrations.storage import get_object_storage
from app.integrations.storage.ports import ObjectStorage

router = APIRouter(
    prefix="/orgs/{orgId}/brands/{brandId}/posts", tags=["posts"]
)


def _actor(ctx: AccessContext) -> posts.Actor:
    return posts.user_actor(user_id=ctx.user_id, name=ctx.user_name)


@router.get("", response_model=list[PostOut], response_model_exclude_none=True)
async def list_posts(
    brandId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    platform: Annotated[str | None, Query()] = None,
    scheduled_from: Annotated[datetime | None, Query(alias="from")] = None,
    scheduled_to: Annotated[datetime | None, Query(alias="to")] = None,
) -> list[PostOut]:
    return await posts.list_posts(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        storage=storage,
        status=status_filter,
        platform=platform,
        scheduled_from=scheduled_from,
        scheduled_to=scheduled_to,
    )


@router.get(
    "/approval-queue",
    response_model=list[PostOut],
    response_model_exclude_none=True,
)
async def approval_queue(
    brandId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> list[PostOut]:
    return await posts.list_approval_queue(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        storage=storage,
    )


@router.post(
    "/approve",
    response_model=list[BulkApproveItem],
    response_model_exclude_none=True,
)
async def bulk_approve(
    brandId: uuid.UUID,  # noqa: N803
    body: BulkApproveBody,
    ctx: Annotated[AccessContext, Depends(require_capability("post.approve"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> list[BulkApproveItem]:
    return await posts.approve_many(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_ids=body.post_ids,
        actor=_actor(ctx),
        storage=storage,
    )


@router.post(
    "/comments/{commentId}/resolve",
    response_model=PostCommentOut,
    response_model_exclude_none=True,
)
async def resolve_comment(
    brandId: uuid.UUID,  # noqa: N803
    commentId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.comment"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> PostCommentOut:
    return await posts.resolve_comment(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        comment_id=commentId,
        actor=_actor(ctx),
    )


@router.post(
    "",
    response_model=PostOut,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def create_post(
    brandId: uuid.UUID,  # noqa: N803
    body: CreatePostBody,
    ctx: Annotated[AccessContext, Depends(require_capability("post.create"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> PostOut:
    return await posts.create_post(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        body=body,
        actor=_actor(ctx),
        storage=storage,
    )


@router.get("/{postId}", response_model=PostOut, response_model_exclude_none=True)
async def get_post(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> PostOut:
    post = await posts.get_post(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        storage=storage,
    )
    if post is None:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)
    return post


@router.patch("/{postId}", response_model=PostOut, response_model_exclude_none=True)
async def update_post(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    body: UpdatePostBody,
    ctx: Annotated[AccessContext, Depends(require_capability("post.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> PostOut:
    return await posts.update_post(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        body=body,
        actor=_actor(ctx),
        storage=storage,
    )


@router.post(
    "/{postId}/duplicate",
    response_model=PostOut,
    response_model_exclude_none=True,
)
async def duplicate_post(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.create"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> PostOut:
    return await posts.duplicate_post(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        actor=_actor(ctx),
        storage=storage,
    )


@router.get(
    "/{postId}/group",
    response_model=list[PostOut],
    response_model_exclude_none=True,
)
async def get_post_group(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> list[PostOut]:
    return await posts.get_post_group(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        storage=storage,
    )


@router.get(
    "/{postId}/versions",
    response_model=list[PostVersionOut],
    response_model_exclude_none=True,
)
async def list_versions(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[PostVersionOut]:
    return await posts.list_versions(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
    )


@router.get(
    "/{postId}/comments",
    response_model=list[PostCommentOut],
    response_model_exclude_none=True,
)
async def list_comments(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[PostCommentOut]:
    return await posts.list_comments(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
    )


@router.post(
    "/{postId}/comments",
    response_model=PostCommentOut,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def add_comment(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    body: AddCommentBody,
    ctx: Annotated[AccessContext, Depends(require_capability("post.comment"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> PostCommentOut:
    return await posts.add_comment(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        body=body,
        actor=_actor(ctx),
    )


@router.post(
    "/{postId}/submit",
    response_model=PostOut,
    response_model_exclude_none=True,
)
async def submit_for_review(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.submit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> PostOut:
    return await posts.submit_for_review(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        actor=_actor(ctx),
        storage=storage,
    )


@router.post(
    "/{postId}/resubmit",
    response_model=PostOut,
    response_model_exclude_none=True,
)
async def resubmit(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.submit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> PostOut:
    return await posts.resubmit(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        actor=_actor(ctx),
        storage=storage,
    )


@router.post(
    "/{postId}/withdraw",
    response_model=PostOut,
    response_model_exclude_none=True,
)
async def withdraw(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.submit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> PostOut:
    return await posts.withdraw(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        actor=_actor(ctx),
        storage=storage,
    )


@router.post(
    "/{postId}/request-changes",
    response_model=PostOut,
    response_model_exclude_none=True,
)
async def request_changes(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    body: ReasonBody,
    ctx: Annotated[AccessContext, Depends(require_capability("post.requestChanges"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> PostOut:
    return await posts.request_changes(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        body=body,
        actor=_actor(ctx),
        storage=storage,
    )


@router.post(
    "/{postId}/reject",
    response_model=PostOut,
    response_model_exclude_none=True,
)
async def reject_post(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    body: ReasonBody,
    ctx: Annotated[AccessContext, Depends(require_capability("post.reject"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> PostOut:
    return await posts.reject_post(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        body=body,
        actor=_actor(ctx),
        storage=storage,
    )


@router.post(
    "/{postId}/approve",
    response_model=PostOut,
    response_model_exclude_none=True,
)
async def approve_post(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.approve"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> PostOut:
    return await posts.approve_post(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        actor=_actor(ctx),
        storage=storage,
    )


@router.post(
    "/{postId}/schedule",
    response_model=PostOut,
    response_model_exclude_none=True,
)
async def schedule_post(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.schedule"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> PostOut:
    return await posts.schedule_post(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        actor=_actor(ctx),
        storage=storage,
    )


@router.post(
    "/{postId}/unschedule",
    response_model=PostOut,
    response_model_exclude_none=True,
)
async def unschedule_post(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.unschedule"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> PostOut:
    return await posts.unschedule_post(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        actor=_actor(ctx),
        storage=storage,
    )


@router.post(
    "/{postId}/reschedule",
    response_model=RescheduleResponse,
    response_model_exclude_none=True,
)
async def reschedule_post(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    body: RescheduleBody,
    ctx: Annotated[AccessContext, Depends(require_capability("post.schedule"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> RescheduleResponse:
    return await posts.reschedule_post(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        date_key=body.date_key,
        actor=_actor(ctx),
        storage=storage,
    )
