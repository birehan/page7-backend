"""Inbox HTTP routes — org-scoped conversations and saved replies."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import AccessContext, require_capability, require_membership
from app.db.session import get_db_session
from app.features.inbox import service as inbox
from app.features.inbox.schemas import (
    AiDraftOut,
    AssignConversationBody,
    ConversationMessagePage,
    ConversationOut,
    ConversationPage,
    CreateSavedReplyBody,
    InboxSyncBody,
    InboxSyncEnqueueOut,
    InboxSyncStatusOut,
    SavedReplyOut,
    SendReplyBody,
    SetConversationTagsBody,
)
from app.infrastructure.idempotency.dependency import (
    IdempotencyContext,
    replay_response,
    require_idempotency,
    serialize_model,
)
from app.integrations.llm import get_llm_router
from app.integrations.llm.router import LLMTaskRouter

router = APIRouter(prefix="/orgs/{orgId}/inbox", tags=["inbox"])


@router.post(
    "/sync",
    response_model=InboxSyncEnqueueOut,
    response_model_exclude_none=True,
)
async def trigger_inbox_sync(
    ctx: Annotated[AccessContext, Depends(require_capability("post.comment"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    body: InboxSyncBody | None = None,
) -> InboxSyncEnqueueOut:
    payload = body or InboxSyncBody()
    return await inbox.trigger_org_sync(
        db,
        organization_id=ctx.organization_id,
        brand_id=payload.brand_id,
        account_id=payload.account_id,
        skip_classify=payload.skip_classify,
        conversation_limit=payload.conversation_limit,
        mode=payload.mode,
    )


@router.get(
    "/sync-status",
    response_model=InboxSyncStatusOut,
    response_model_exclude_none=True,
)
async def get_inbox_sync_status(
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> InboxSyncStatusOut:
    return await inbox.get_org_sync_status(db, organization_id=ctx.organization_id)


@router.get(
    "/conversations",
    response_model=ConversationPage,
    response_model_exclude_none=True,
)
async def list_conversations(
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=30, ge=1, le=100),
    status_filter: Annotated[
        Literal["open", "resolved"] | None, Query(alias="status")
    ] = None,
    platform: str | None = Query(default=None),
    assignee_id: Annotated[uuid.UUID | None, Query(alias="assigneeId")] = None,
    unassigned: bool = Query(default=False),
    tag: str | None = Query(default=None),
) -> ConversationPage:
    return await inbox.list_conversations_page(
        db,
        organization_id=ctx.organization_id,
        status=status_filter,
        platform=platform,
        assignee_id=assignee_id,
        unassigned=unassigned,
        tag=tag,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/conversations/{conversationId}/messages",
    response_model=ConversationMessagePage,
    response_model_exclude_none=True,
)
async def list_messages(
    conversationId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
) -> ConversationMessagePage:
    return await inbox.list_messages_page(
        db,
        organization_id=ctx.organization_id,
        conversation_id=conversationId,
        cursor=cursor,
        limit=limit,
    )


@router.post(
    "/conversations/{conversationId}/ai-draft",
    response_model=AiDraftOut,
    response_model_exclude_none=True,
)
async def draft_conversation_reply(
    conversationId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.comment"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    llm_router: Annotated[LLMTaskRouter, Depends(get_llm_router)],
) -> AiDraftOut:
    from app.features import billing as billing_feature

    await billing_feature.enforce_ai_credit_limit(
        db, organization_id=ctx.organization_id
    )
    return await inbox.draft_reply(
        db,
        organization_id=ctx.organization_id,
        conversation_id=conversationId,
        actor_user_id=ctx.user_id,
        router=llm_router,
    )


@router.post(
    "/conversations/{conversationId}/reply",
    response_model=ConversationOut,
    response_model_exclude_none=True,
)
async def reply_to_conversation(
    conversationId: uuid.UUID,  # noqa: N803
    body: SendReplyBody,
    response: Response,
    ctx: Annotated[AccessContext, Depends(require_capability("post.comment"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    idem: Annotated[
        IdempotencyContext, Depends(require_idempotency("inbox.reply"))
    ],
) -> ConversationOut | dict[str, object]:
    if idem.is_replay:
        return replay_response(idem, response)

    conversation = await inbox.reply_and_return_conversation(
        db,
        organization_id=ctx.organization_id,
        conversation_id=conversationId,
        body=body.body,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
    )
    await idem.complete(status_code=200, body=serialize_model(conversation))
    return conversation


@router.post(
    "/conversations/{conversationId}/assign",
    response_model=ConversationOut,
    response_model_exclude_none=True,
)
async def assign_conversation(
    conversationId: uuid.UUID,  # noqa: N803
    body: AssignConversationBody,
    ctx: Annotated[AccessContext, Depends(require_capability("post.comment"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ConversationOut:
    return await inbox.assign_conversation(
        db,
        organization_id=ctx.organization_id,
        conversation_id=conversationId,
        assignee_id=body.assignee_id,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
    )


@router.put(
    "/conversations/{conversationId}/tags",
    response_model=ConversationOut,
    response_model_exclude_none=True,
)
async def set_conversation_tags(
    conversationId: uuid.UUID,  # noqa: N803
    body: SetConversationTagsBody,
    ctx: Annotated[AccessContext, Depends(require_capability("post.comment"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ConversationOut:
    return await inbox.set_conversation_tags(
        db,
        organization_id=ctx.organization_id,
        conversation_id=conversationId,
        tags=body.tags,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
    )


@router.post(
    "/conversations/{conversationId}/resolve",
    response_model=ConversationOut,
    response_model_exclude_none=True,
)
async def resolve_conversation(
    conversationId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.comment"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ConversationOut:
    return await inbox.resolve_conversation(
        db,
        organization_id=ctx.organization_id,
        conversation_id=conversationId,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
    )


@router.post(
    "/conversations/{conversationId}/reopen",
    response_model=ConversationOut,
    response_model_exclude_none=True,
)
async def reopen_conversation(
    conversationId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.comment"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ConversationOut:
    return await inbox.reopen_conversation(
        db,
        organization_id=ctx.organization_id,
        conversation_id=conversationId,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
    )


@router.post(
    "/conversations/{conversationId}/escalate",
    response_model=ConversationOut,
    response_model_exclude_none=True,
)
async def escalate_conversation(
    conversationId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.comment"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> ConversationOut:
    return await inbox.escalate_conversation(
        db,
        organization_id=ctx.organization_id,
        conversation_id=conversationId,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
    )


@router.get(
    "/saved-replies",
    response_model=list[SavedReplyOut],
    response_model_exclude_none=True,
)
async def list_saved_replies(
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[SavedReplyOut]:
    return await inbox.list_saved_replies_out(
        db, organization_id=ctx.organization_id
    )


@router.post(
    "/saved-replies",
    response_model=SavedReplyOut,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def create_saved_reply(
    body: CreateSavedReplyBody,
    ctx: Annotated[AccessContext, Depends(require_capability("post.comment"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> SavedReplyOut:
    return await inbox.create_saved_reply_out(
        db,
        organization_id=ctx.organization_id,
        title=body.title,
        body_ar=body.body_ar,
        body_en=body.body_en,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
    )


@router.delete(
    "/saved-replies/{savedReplyId}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_saved_reply(
    savedReplyId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.comment"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    await inbox.delete_saved_reply_out(
        db,
        organization_id=ctx.organization_id,
        saved_reply_id=savedReplyId,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
