"""Inbox service — webhook ingest, classify, sync backfill, exactly-once reply."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.time import utc_now
from app.features import brands
from app.features.content_ai import insert_decision
from app.features.inbox import repository
from app.features.inbox.exceptions import ConversationNotFound
from app.features.inbox.models import Conversation, ConversationMessage
from app.features.inbox.prompts import classification as classification_prompt
from app.features.inbox.prompts import reply_suggestion as reply_prompt
from app.features.inbox.schemas import (
    AiDraftOut,
    ConversationMessageOut,
    ConversationMessagePage,
    ConversationOut,
    ConversationPage,
    ConversationParticipantOut,
    InboxSyncAccountStatusOut,
    InboxSyncEnqueueOut,
    InboxSyncStatusOut,
    SavedReplyOut,
)
from app.features.social_accounts import (
    SocialAccount,
    get_account_by_zernio_id,
    get_credential,
)
from app.integrations.llm import get_llm_router
from app.integrations.llm.ports import LLMMessage, StructuredGenerationRequest
from app.integrations.llm.router import LLMTaskRouter
from app.integrations.social import get_social_provider_for_credential
from app.integrations.social.ports import InboxSendResult, SocialProvider
from app.jobs import queue as job_queue
from app.jobs.errors import RetryableError, TerminalError

log = structlog.get_logger(__name__)

OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
RECONCILE_PENDING_AGE = timedelta(seconds=60)
RECONCILE_FAIL_AFTER = timedelta(minutes=30)
REPLY_DRAFT_MESSAGE_LIMIT = 20


class SentimentOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sentiment: str


class ReplySuggestionOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suggested_reply_ar: str
    suggested_reply_en: str


# ---------------------------------------------------------------------------
# Webhook ingest
# ---------------------------------------------------------------------------


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _first_str(*candidates: Any) -> str:
    for value in candidates:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _normalize_inbound_payload(
    event_type: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Flatten both our synthetic `data{}` shape and live Zernio nested shapes.

    Live ``comment.received`` (observed 2026-09-05) nests fields under
    ``account`` / ``comment`` / ``post`` with ``comment.text`` for the body.
    Synthetic / fake-server payloads keep a flat ``data`` object.
    """
    data = _as_dict(payload.get("data")) or payload
    account = _as_dict(data.get("account"))
    comment = _as_dict(data.get("comment"))
    post = _as_dict(data.get("post"))
    message = _as_dict(data.get("message"))
    author = _as_dict(
        data.get("author")
        or comment.get("author")
        or message.get("author")
        or data.get("from")
    )

    kind = "comment" if event_type == "comment.received" else "dm"
    if kind == "comment":
        external_thread_id = _first_str(
            data.get("postId"),
            data.get("post_id"),
            data.get("threadId"),
            comment.get("postId"),
            post.get("id"),
        )
        external_message_id = _first_str(
            data.get("commentId"),
            data.get("comment_id"),
            data.get("messageId"),
            data.get("message_id"),
            comment.get("id"),
            data.get("id"),
        )
        body = _first_str(
            data.get("message"),
            data.get("text"),
            data.get("body"),
            comment.get("text"),
            comment.get("message"),
        )
        own_account = bool(author.get("isOwnAccount") or comment.get("isOwnAccount"))
    else:
        external_thread_id = _first_str(
            data.get("conversationId"),
            data.get("conversation_id"),
            data.get("threadId"),
            data.get("thread_id"),
            message.get("conversationId"),
            data.get("id") if data.get("type") == "conversation" else None,
        )
        external_message_id = _first_str(
            data.get("messageId"),
            data.get("message_id"),
            message.get("id"),
            data.get("id"),
        )
        body = _first_str(
            data.get("message"),
            data.get("text"),
            data.get("body"),
            message.get("text"),
            message.get("body"),
        )
        own_account = bool(author.get("isOwnAccount") or message.get("isOwnAccount"))

    return {
        "kind": kind,
        "zernio_account_id": _first_str(
            data.get("accountId"),
            data.get("account_id"),
            account.get("id"),
            account.get("accountId"),
        ),
        "external_thread_id": external_thread_id,
        "external_message_id": external_message_id or None,
        "body": body,
        "participant_name": _first_str(
            data.get("authorName"),
            data.get("author_name"),
            author.get("name"),
            data.get("participantName"),
            "Unknown",
        ),
        "participant_handle": _first_str(
            data.get("authorHandle"),
            data.get("author_handle"),
            author.get("username"),
            author.get("handle"),
        )
        or None,
        "participant_external_id": _first_str(
            author.get("id"),
            data.get("authorId"),
            data.get("author_id"),
        )
        or None,
        "platform": _first_str(
            data.get("platform"),
            account.get("platform"),
            comment.get("platform"),
            message.get("platform"),
        )
        or None,
        "own_account": own_account,
    }


async def apply_inbound_webhook(
    session: AsyncSession,
    *,
    event_type: str,
    payload: dict[str, Any],
) -> ConversationMessage | None:
    """Upsert conversation + insert message for comment.received / message.received."""
    fields = _normalize_inbound_payload(event_type, payload)
    zernio_account_id = fields["zernio_account_id"]
    if not zernio_account_id:
        log.info("inbox.inbound_missing_account")
        return None

    account = await get_account_by_zernio_id(
        session, zernio_account_id=zernio_account_id
    )
    if account is None:
        log.info("inbox.inbound_unknown_account", zernio_account_id=zernio_account_id)
        return None

    # Echo of our own outbound comment/DM — do not open a new inbound thread.
    if fields["own_account"]:
        log.info(
            "inbox.inbound_skip_own_account",
            event_type=event_type,
            zernio_account_id=zernio_account_id,
        )
        return None

    kind = str(fields["kind"])
    external_thread_id = str(fields["external_thread_id"])
    if not external_thread_id:
        log.info("inbox.inbound_missing_thread", event_type=event_type)
        return None

    external_message_id = fields["external_message_id"]
    body = str(fields["body"])
    if not body:
        log.info("inbox.inbound_empty_body", event_type=event_type)
        return None

    participant_name = str(fields["participant_name"])
    participant_handle = fields["participant_handle"]
    participant_external_id = fields["participant_external_id"]
    platform = account.platform or str(fields["platform"] or "instagram")

    conversation = await repository.upsert_conversation(
        session,
        organization_id=account.organization_id,
        brand_id=account.brand_id,
        social_account_id=account.id,
        platform=platform,
        kind=kind,
        external_thread_id=external_thread_id,
        participant_name=participant_name,
        participant_handle=participant_handle,
        participant_external_id=participant_external_id,
    )

    lang = _guess_lang(body)
    message = await repository.insert_inbound_message(
        session,
        organization_id=account.organization_id,
        conversation_id=conversation.id,
        body=body,
        lang=lang,
        author_name=participant_name,
        external_message_id=external_message_id,
    )
    if message is None:
        log.info(
            "inbox.inbound_duplicate_message",
            conversation_id=str(conversation.id),
            external_message_id=external_message_id,
        )
        return None

    await job_queue.enqueue(
        session,
        queue="ai",
        type="inbox_classify_message",
        payload={"message_id": str(message.id)},
        unique_key=f"inbox_classify:{message.id}",
        organization_id=account.organization_id,
    )
    return message


async def apply_conversation_started(
    session: AsyncSession, *, payload: dict[str, Any]
) -> Conversation | None:
    """Upsert an empty/new DM thread from conversation.started."""
    fields = _normalize_inbound_payload("message.received", payload)
    zernio_account_id = fields["zernio_account_id"]
    external_thread_id = str(fields["external_thread_id"] or "")
    if not zernio_account_id or not external_thread_id:
        log.info("inbox.conversation_started_incomplete")
        return None
    account = await get_account_by_zernio_id(
        session, zernio_account_id=zernio_account_id
    )
    if account is None:
        log.info(
            "inbox.conversation_started_unknown_account",
            zernio_account_id=zernio_account_id,
        )
        return None
    platform = account.platform or str(fields["platform"] or "instagram")
    return await repository.upsert_conversation(
        session,
        organization_id=account.organization_id,
        brand_id=account.brand_id,
        social_account_id=account.id,
        platform=platform,
        kind="dm",
        external_thread_id=external_thread_id,
        participant_name=str(fields["participant_name"]),
        participant_handle=fields["participant_handle"],
        participant_external_id=fields["participant_external_id"],
    )


_OUTBOUND_STATUS_MAP = {
    "message.sent": "sent",
    "message.delivered": "sent",
    "message.read": "sent",
    "message.failed": "failed",
}


async def apply_outbound_status_webhook(
    session: AsyncSession, *, event_type: str, payload: dict[str, Any]
) -> ConversationMessage | None:
    """Update outbound delivery status from message.sent/delivered/read/failed."""
    delivery = _OUTBOUND_STATUS_MAP.get(event_type)
    if delivery is None:
        return None
    data = _as_dict(payload.get("data")) or payload
    message_obj = _as_dict(data.get("message"))
    external_message_id = _first_str(
        data.get("messageId"),
        data.get("message_id"),
        message_obj.get("id"),
        data.get("id"),
    )
    if not external_message_id:
        log.info("inbox.outbound_status_missing_id", event_type=event_type)
        return None
    message = await repository.find_message_by_external_id(
        session, external_message_id=external_message_id
    )
    if message is None or message.direction != "outbound":
        log.info(
            "inbox.outbound_status_unknown_message",
            event_type=event_type,
            external_message_id=external_message_id,
        )
        return None
    error_message = _first_str(data.get("error"), data.get("errorMessage")) or None
    await repository.update_outbound_delivery_status(
        session,
        message=message,
        delivery_status=delivery,
        error_message=error_message,
    )
    return message


# ---------------------------------------------------------------------------
# Classify + suggest
# ---------------------------------------------------------------------------


async def classify_message(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    settings: Settings | None = None,
    router: LLMTaskRouter | None = None,
) -> None:
    cfg = settings or get_settings()
    llm = router or get_llm_router(cfg)

    message = await repository.get_message(session, message_id=message_id)
    if message is None:
        return
    conversation = await repository.get_conversation(
        session, conversation_id=message.conversation_id
    )
    if conversation is None:
        return
    if conversation.sentiment_decision_id is not None and (
        conversation.suggestion_decision_id is not None
    ):
        return

    brand = await brands.get_brand(
        session,
        organization_id=conversation.organization_id,
        brand_id=conversation.brand_id,
    )
    brand_version = brand.version if brand is not None else 1
    brand_name = brand.name if brand is not None else None

    sentiment_messages = classification_prompt.build(
        body=message.body,
        platform=conversation.platform,
        kind=conversation.kind,
    )
    sentiment_req = StructuredGenerationRequest(
        messages=sentiment_messages,
        output_schema=classification_prompt.OUTPUT_SCHEMA,
        schema_name="classification",
    )
    t0 = time.monotonic()
    sentiment_out, sentiment_resp = await _run_structured_validated(
        llm, "classification", sentiment_req, SentimentOut
    )
    task_cfg = cfg.llm.tasks["classification"]
    sentiment_inputs = {
        "messageId": str(message.id),
        "conversationId": str(conversation.id),
    }
    sentiment_decision = await insert_decision(
        session,
        organization_id=conversation.organization_id,
        brand_id=conversation.brand_id,
        brand_version=brand_version,
        actor_user_id=None,
        kind="sentiment",
        provider=task_cfg.provider,
        model=task_cfg.model,
        prompt_version=classification_prompt.PROMPT_VERSION,
        inputs_hash=_inputs_hash(sentiment_inputs),
        input_summary=sentiment_inputs,
        output=sentiment_out.model_dump(),
        status="succeeded",
        target_type="conversation",
        target_id=conversation.id,
        prompt_tokens=sentiment_resp.usage.prompt_tokens,
        completion_tokens=sentiment_resp.usage.completion_tokens,
        cost_usd=sentiment_resp.usage.cost_usd,
        latency_ms=int((time.monotonic() - t0) * 1000),
        provider_request_id=sentiment_resp.usage.provider_request_id,
    )

    recent = await repository.list_recent_messages(
        session,
        conversation_id=conversation.id,
        limit=REPLY_DRAFT_MESSAGE_LIMIT,
    )
    reply_messages = reply_prompt.build(
        messages=_transcript_from_rows(recent),
        platform=conversation.platform,
        kind=conversation.kind,
        sentiment=sentiment_out.sentiment,
        brand_name=brand_name,
    )
    reply_req = StructuredGenerationRequest(
        messages=reply_messages,
        output_schema=reply_prompt.OUTPUT_SCHEMA,
        schema_name="reply_suggestion",
    )
    t1 = time.monotonic()
    reply_out, reply_resp = await _run_structured_validated(
        llm, "reply_suggestion", reply_req, ReplySuggestionOut
    )
    reply_cfg = cfg.llm.tasks["reply_suggestion"]
    reply_inputs = {
        "messageId": str(message.id),
        "conversationId": str(conversation.id),
        "messageCount": len(recent),
        "sentiment": sentiment_out.sentiment,
    }
    suggestion_decision = await insert_decision(
        session,
        organization_id=conversation.organization_id,
        brand_id=conversation.brand_id,
        brand_version=brand_version,
        actor_user_id=None,
        kind="reply_suggest",
        provider=reply_cfg.provider,
        model=reply_cfg.model,
        prompt_version=reply_prompt.PROMPT_VERSION,
        inputs_hash=_inputs_hash(reply_inputs),
        input_summary=reply_inputs,
        output=reply_out.model_dump(),
        status="succeeded",
        target_type="conversation",
        target_id=conversation.id,
        prompt_tokens=reply_resp.usage.prompt_tokens,
        completion_tokens=reply_resp.usage.completion_tokens,
        cost_usd=reply_resp.usage.cost_usd,
        latency_ms=int((time.monotonic() - t1) * 1000),
        provider_request_id=reply_resp.usage.provider_request_id,
    )

    await repository.update_conversation_classification(
        session,
        conversation_id=conversation.id,
        sentiment=sentiment_out.sentiment,
        sentiment_decision_id=sentiment_decision.id,
        suggested_reply_ar=reply_out.suggested_reply_ar,
        suggested_reply_en=reply_out.suggested_reply_en,
        suggestion_decision_id=suggestion_decision.id,
    )


async def draft_reply(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    settings: Settings | None = None,
    router: LLMTaskRouter | None = None,
) -> AiDraftOut:
    """Generate a bilingual draft from the last REPLY_DRAFT_MESSAGE_LIMIT messages."""
    cfg = settings or get_settings()
    llm = router or get_llm_router(cfg)

    conversation = _require_org_conversation(
        await repository.get_conversation(session, conversation_id=conversation_id),
        organization_id=organization_id,
    )
    recent = await repository.list_recent_messages(
        session,
        conversation_id=conversation.id,
        limit=REPLY_DRAFT_MESSAGE_LIMIT,
    )
    brand = await brands.get_brand(
        session,
        organization_id=conversation.organization_id,
        brand_id=conversation.brand_id,
    )
    brand_version = brand.version if brand is not None else 1
    brand_name = brand.name if brand is not None else None
    sentiment = (
        conversation.sentiment
        if conversation.sentiment in ("positive", "neutral", "negative")
        else "neutral"
    )

    reply_messages = reply_prompt.build(
        messages=_transcript_from_rows(recent),
        platform=conversation.platform,
        kind=conversation.kind,
        sentiment=sentiment,
        brand_name=brand_name,
    )
    reply_req = StructuredGenerationRequest(
        messages=reply_messages,
        output_schema=reply_prompt.OUTPUT_SCHEMA,
        schema_name="reply_suggestion",
    )
    t0 = time.monotonic()
    reply_out, reply_resp = await _run_structured_validated(
        llm, "reply_suggestion", reply_req, ReplySuggestionOut
    )
    reply_cfg = cfg.llm.tasks["reply_suggestion"]
    reply_inputs = {
        "conversationId": str(conversation.id),
        "messageCount": len(recent),
        "sentiment": sentiment,
        "source": "ai_draft",
    }
    suggestion_decision = await insert_decision(
        session,
        organization_id=conversation.organization_id,
        brand_id=conversation.brand_id,
        brand_version=brand_version,
        actor_user_id=actor_user_id,
        kind="reply_suggest",
        provider=reply_cfg.provider,
        model=reply_cfg.model,
        prompt_version=reply_prompt.PROMPT_VERSION,
        inputs_hash=_inputs_hash(reply_inputs),
        input_summary=reply_inputs,
        output=reply_out.model_dump(),
        status="succeeded",
        target_type="conversation",
        target_id=conversation.id,
        prompt_tokens=reply_resp.usage.prompt_tokens,
        completion_tokens=reply_resp.usage.completion_tokens,
        cost_usd=reply_resp.usage.cost_usd,
        latency_ms=int((time.monotonic() - t0) * 1000),
        provider_request_id=reply_resp.usage.provider_request_id,
    )
    await repository.update_suggested_replies(
        session,
        conversation_id=conversation.id,
        suggested_reply_ar=reply_out.suggested_reply_ar,
        suggested_reply_en=reply_out.suggested_reply_en,
        suggestion_decision_id=suggestion_decision.id,
    )
    return AiDraftOut(
        suggested_reply_ar=reply_out.suggested_reply_ar,
        suggested_reply_en=reply_out.suggested_reply_en,
    )


# ---------------------------------------------------------------------------
# Sync backfill
# ---------------------------------------------------------------------------

_INBOX_PLATFORMS = frozenset({"instagram", "facebook", "whatsapp", "snapchat"})
_MAX_CONV_PAGES = 3
_CONV_PAGE_SIZE = 50
_MSG_PAGE_SIZE = 100
_MAX_MSG_PAGES_PER_CONV = 3
_MAX_MESSAGES_PER_RUN = 500
_MAX_COMMENT_POSTS = 20
_COMMENT_LOOKBACK_DAYS = 30
_SYNC_BUCKET_MINUTES = 5
_DEFAULT_MANUAL_CONV_LIMIT = 25
_SyncMode = Literal["initial", "more", "poll"]


def _sync_time_bucket(now: datetime | None = None) -> str:
    ts = now or utc_now()
    floored_minute = (ts.minute // _SYNC_BUCKET_MINUTES) * _SYNC_BUCKET_MINUTES
    bucket = ts.replace(minute=floored_minute, second=0, microsecond=0)
    return bucket.strftime("%Y%m%d%H%M")


def inbox_sync_unique_key(
    account_id: uuid.UUID, *, now: datetime | None = None
) -> str:
    """Time-bucketed unique key so completed syncs can re-run every 5 minutes."""
    return f"inbox_sync:{account_id}:{_sync_time_bucket(now)}"


def inbox_sync_more_unique_key(
    account_id: uuid.UUID,
    *,
    dm_cursor: str | None,
    now: datetime | None = None,
) -> str:
    """Unique key for Sync-more pages; cursor hash allows paging within a bucket."""
    cursor_digest = hashlib.sha256((dm_cursor or "").encode()).hexdigest()[:12]
    return f"inbox_sync:more:{account_id}:{cursor_digest}:{_sync_time_bucket(now)}"


async def enqueue_inbox_sync(
    session: AsyncSession,
    *,
    social_account_id: uuid.UUID,
    organization_id: uuid.UUID,
    skip_classify: bool = True,
    conversation_limit: int | None = None,
    mode: _SyncMode = "poll",
) -> int | None:
    payload: dict[str, Any] = {
        "social_account_id": str(social_account_id),
        "skip_classify": skip_classify,
        "mode": mode,
    }
    if conversation_limit is not None:
        payload["conversation_limit"] = conversation_limit

    if mode == "more":
        state = await repository.get_or_create_sync_state(
            session, social_account_id=social_account_id
        )
        unique_key = inbox_sync_more_unique_key(
            social_account_id, dm_cursor=state.dm_cursor
        )
    else:
        unique_key = inbox_sync_unique_key(social_account_id)

    return await job_queue.enqueue(
        session,
        queue="sync",
        type="inbox_sync",
        payload=payload,
        unique_key=unique_key,
        organization_id=organization_id,
    )


async def enqueue_inbox_poll(session: AsyncSession) -> int:
    """Enqueue inbox_sync for every connected inbox-capable account."""
    import sqlalchemy as sa

    from app.features.social_accounts import SocialAccount

    stmt = sa.select(SocialAccount).where(
        SocialAccount.status.in_(("connected", "expiring")),
        SocialAccount.zernio_account_id.is_not(None),
        SocialAccount.platform.in_(tuple(_INBOX_PLATFORMS)),
    )
    accounts = list((await session.execute(stmt)).scalars().all())
    enqueued = 0
    for account in accounts:
        job_id = await enqueue_inbox_sync(
            session,
            social_account_id=account.id,
            organization_id=account.organization_id,
        )
        if job_id is not None:
            enqueued += 1
    return enqueued


async def enqueue_org_inbox_sync(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    skip_classify: bool = True,
    conversation_limit: int = _DEFAULT_MANUAL_CONV_LIMIT,
    mode: _SyncMode = "initial",
) -> list[uuid.UUID]:
    """Enqueue sync for connected org accounts; return account ids enqueued."""
    import sqlalchemy as sa

    from app.features.social_accounts import SocialAccount

    stmt = sa.select(SocialAccount).where(
        SocialAccount.organization_id == organization_id,
        SocialAccount.status.in_(("connected", "expiring")),
        SocialAccount.zernio_account_id.is_not(None),
        SocialAccount.platform.in_(tuple(_INBOX_PLATFORMS)),
    )
    if brand_id is not None:
        stmt = stmt.where(SocialAccount.brand_id == brand_id)
    if account_id is not None:
        stmt = stmt.where(SocialAccount.id == account_id)
    accounts = list((await session.execute(stmt)).scalars().all())
    enqueued_ids: list[uuid.UUID] = []
    for account in accounts:
        if mode == "more":
            state = await repository.get_or_create_sync_state(
                session, social_account_id=account.id
            )
            if not state.dm_cursor:
                continue
        job_id = await enqueue_inbox_sync(
            session,
            social_account_id=account.id,
            organization_id=account.organization_id,
            skip_classify=skip_classify,
            conversation_limit=conversation_limit,
            mode=mode,
        )
        if job_id is not None:
            enqueued_ids.append(account.id)
    return enqueued_ids


async def sync_backfill(
    session: AsyncSession,
    *,
    social_account_id: uuid.UUID,
    settings: Settings | None = None,
    provider: SocialProvider | None = None,
    skip_classify: bool = True,
    conversation_limit: int | None = None,
    mode: _SyncMode = "poll",
) -> int:
    """Backfill DM conversations (hydrated) + comments on recent published posts."""
    cfg = settings or get_settings()
    account = await session.get(SocialAccount, social_account_id)
    if account is None or not account.zernio_account_id or account.credential_id is None:
        return 0
    if account.platform not in _INBOX_PLATFORMS:
        return 0

    state = await repository.get_or_create_sync_state(
        session, social_account_id=account.id
    )

    if provider is None:
        credential = await get_credential(session, credential_id=account.credential_id)
        if credential is None:
            await repository.mark_sync_error(
                session, state=state, error="credential missing"
            )
            return 0
        provider = get_social_provider_for_credential(
            cfg, alias=credential.alias, secret_ref=credential.secret_ref
        )

    if mode in ("initial", "more"):
        page_size = conversation_limit or _DEFAULT_MANUAL_CONV_LIMIT
        max_pages = 1
        start_cursor = state.dm_cursor if mode == "more" else None
        persist_cursor = True
    else:
        page_size = _CONV_PAGE_SIZE
        max_pages = _MAX_CONV_PAGES
        start_cursor = None
        persist_cursor = False

    upserted = 0
    messages_fetched = 0
    next_dm_cursor: str | None = None
    try:
        upserted, messages_fetched, next_dm_cursor = await _sync_dm_conversations(
            session,
            account=account,
            provider=provider,
            messages_budget=_MAX_MESSAGES_PER_RUN,
            skip_classify=skip_classify,
            page_size=page_size,
            max_pages=max_pages,
            start_cursor=start_cursor,
        )
        comment_upserted, comment_msgs = await _sync_post_comments(
            session,
            account=account,
            provider=provider,
            messages_budget=max(0, _MAX_MESSAGES_PER_RUN - messages_fetched),
            skip_classify=skip_classify,
        )
        upserted += comment_upserted
        messages_fetched += comment_msgs
        await repository.mark_sync_ok(
            session,
            state=state,
            dm_cursor=next_dm_cursor,
            update_dm_cursor=persist_cursor,
        )
    except Exception as exc:
        await repository.mark_sync_error(session, state=state, error=str(exc))
        raise

    log.info(
        "inbox.sync_backfill.done",
        social_account_id=str(account.id),
        conversations=upserted,
        messages_fetched=messages_fetched,
        mode=mode,
        skip_classify=skip_classify,
        has_more=bool(next_dm_cursor) if persist_cursor else None,
    )
    return upserted


async def _sync_dm_conversations(
    session: AsyncSession,
    *,
    account: SocialAccount,
    provider: SocialProvider,
    messages_budget: int,
    skip_classify: bool = True,
    page_size: int = _CONV_PAGE_SIZE,
    max_pages: int = _MAX_CONV_PAGES,
    start_cursor: str | None = None,
) -> tuple[int, int, str | None]:
    assert account.zernio_account_id is not None
    upserted = 0
    messages_fetched = 0
    cursor: str | None = start_cursor
    next_cursor: str | None = None
    for _page in range(max_pages):
        result = await provider.list_inbox_conversations(
            account_id=account.zernio_account_id,
            limit=page_size,
            cursor=cursor,
        )
        for conv in result.conversations:
            platform = conv.platform or account.platform or "instagram"
            remote_last = _parse_ts(conv.last_message_at) or utc_now()
            existing = await repository.get_conversation_by_external(
                session,
                social_account_id=account.id,
                external_thread_id=conv.id,
            )
            local_count = existing.message_count if existing is not None else 0
            local_last = existing.last_message_at if existing is not None else None
            needs_hydrate = local_count == 0 or (
                local_last is not None and remote_last > local_last
            ) or existing is None

            conversation = await repository.upsert_conversation(
                session,
                organization_id=account.organization_id,
                brand_id=account.brand_id,
                social_account_id=account.id,
                platform=platform,
                kind="dm",
                external_thread_id=conv.id,
                participant_name=conv.participant_name or "Unknown",
                participant_handle=conv.participant_handle,
                last_message_at=remote_last,
            )
            upserted += 1
            if not needs_hydrate:
                continue
            if messages_fetched >= messages_budget:
                break
            fetched = await _hydrate_dm_messages(
                session,
                account=account,
                provider=provider,
                conversation=conversation,
                zernio_conversation_id=conv.id,
                budget=messages_budget - messages_fetched,
                skip_classify=skip_classify,
            )
            messages_fetched += fetched
        if messages_fetched >= messages_budget:
            next_cursor = result.next_cursor if result.has_more else None
            break
        if not result.has_more or not result.next_cursor:
            next_cursor = None
            break
        cursor = result.next_cursor
        next_cursor = result.next_cursor
    else:
        # Exhausted max_pages while provider still has more.
        next_cursor = cursor if cursor and cursor != start_cursor else next_cursor
    return upserted, messages_fetched, next_cursor


async def _hydrate_dm_messages(
    session: AsyncSession,
    *,
    account: SocialAccount,
    provider: SocialProvider,
    conversation: Conversation,
    zernio_conversation_id: str,
    budget: int,
    skip_classify: bool = True,
) -> int:
    assert account.zernio_account_id is not None
    fetched = 0
    cursor: str | None = None
    for _page in range(_MAX_MSG_PAGES_PER_CONV):
        if fetched >= budget:
            break
        page = await provider.list_inbox_messages(
            conversation_id=zernio_conversation_id,
            account_id=account.zernio_account_id,
            limit=min(_MSG_PAGE_SIZE, budget - fetched),
            cursor=cursor,
            sort_order="asc",
        )
        for msg in page.messages:
            if fetched >= budget:
                break
            if not msg.id or not msg.text.strip():
                continue
            direction = "outbound" if msg.direction == "outgoing" else "inbound"
            author = (
                msg.sender_name
                or (conversation.participant_name if direction == "inbound" else "Brand")
            )
            inserted = await repository.insert_synced_message(
                session,
                organization_id=account.organization_id,
                conversation_id=conversation.id,
                direction=direction,
                body=msg.text,
                lang=_guess_lang(msg.text),
                author_name=author,
                external_message_id=msg.id,
                created_at=_parse_ts(msg.created_at),
            )
            if inserted is None:
                # Duplicate external_message_id — do not burn the sync budget.
                continue
            fetched += 1
            if direction == "inbound" and not skip_classify:
                await job_queue.enqueue(
                    session,
                    queue="ai",
                    type="inbox_classify_message",
                    payload={"message_id": str(inserted.id)},
                    unique_key=f"inbox_classify:{inserted.id}",
                    organization_id=account.organization_id,
                )
        if not page.has_more or not page.next_cursor:
            break
        cursor = page.next_cursor
    return fetched


async def _sync_post_comments(
    session: AsyncSession,
    *,
    account: SocialAccount,
    provider: SocialProvider,
    messages_budget: int,
    skip_classify: bool = True,
) -> tuple[int, int]:
    assert account.zernio_account_id is not None
    if messages_budget <= 0:
        return 0, 0
    since = utc_now() - timedelta(days=_COMMENT_LOOKBACK_DAYS)
    posts = await repository.list_recent_published_posts(
        session,
        brand_id=account.brand_id,
        platform=account.platform,
        since=since,
        limit=_MAX_COMMENT_POSTS,
    )
    upserted = 0
    messages_fetched = 0
    for post in posts:
        if messages_fetched >= messages_budget:
            break
        zernio_post_id = post.zernio_post_id
        if not zernio_post_id:
            continue
        try:
            comments = await provider.get_post_comments(
                post_id=zernio_post_id, account_id=account.zernio_account_id
            )
        except Exception:
            log.warning(
                "inbox.sync_comments.failed",
                post_id=str(post.id),
                zernio_post_id=zernio_post_id,
                exc_info=True,
            )
            continue
        if not comments:
            continue
        # Seed participant from the first comment; later upserts refresh names.
        first = comments[0]
        conversation = await repository.upsert_conversation(
            session,
            organization_id=account.organization_id,
            brand_id=account.brand_id,
            social_account_id=account.id,
            platform=account.platform,
            kind="comment",
            external_thread_id=zernio_post_id,
            participant_name=first.author_name or "Unknown",
            participant_handle=first.author_handle,
            post_id=post.id,
            last_message_at=_parse_ts(first.created_at) or utc_now(),
        )
        upserted += 1
        for comment in comments:
            if messages_fetched >= messages_budget:
                break
            if not comment.id or not comment.message.strip():
                continue
            inserted = await repository.insert_synced_message(
                session,
                organization_id=account.organization_id,
                conversation_id=conversation.id,
                direction="inbound",
                body=comment.message,
                lang=_guess_lang(comment.message),
                author_name=comment.author_name or "Unknown",
                external_message_id=comment.id,
                created_at=_parse_ts(comment.created_at),
            )
            if inserted is None:
                continue
            messages_fetched += 1
            if not skip_classify:
                await job_queue.enqueue(
                    session,
                    queue="ai",
                    type="inbox_classify_message",
                    payload={"message_id": str(inserted.id)},
                    unique_key=f"inbox_classify:{inserted.id}",
                    organization_id=account.organization_id,
                )
    return upserted, messages_fetched


# ---------------------------------------------------------------------------
# Reply send
# ---------------------------------------------------------------------------


async def send_reply(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    body: str,
    author_user_id: uuid.UUID | None,
    author_name: str,
    lang: str = "en",
) -> ConversationMessage:
    conversation = await repository.get_conversation(
        session, conversation_id=conversation_id
    )
    if conversation is None:
        raise ConversationNotFound()

    message = await repository.insert_outbound_pending(
        session,
        organization_id=conversation.organization_id,
        conversation_id=conversation.id,
        body=body,
        lang=lang if lang in ("ar", "en") else "en",
        author_user_id=author_user_id,
        author_name=author_name,
    )
    await job_queue.enqueue(
        session,
        queue="sync",
        type="inbox_send_reply",
        payload={"message_id": str(message.id)},
        unique_key=f"inbox_reply:{message.id}",
        organization_id=conversation.organization_id,
    )
    return message


async def deliver_outbound_reply(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    settings: Settings | None = None,
    provider: SocialProvider | None = None,
) -> None:
    """Execute provider send for a pending outbound message (exactly-once)."""
    cfg = settings or get_settings()
    message = await repository.get_message(session, message_id=message_id)
    if message is None:
        return
    if message.delivery_status == "sent":
        return
    if message.delivery_status == "failed":
        return
    # Ambiguous prior attempt — never blind-retry the send.
    if message.error_code == OUTCOME_UNKNOWN:
        log.info(
            "inbox_send_reply.skip_unknown",
            message_id=str(message_id),
        )
        return

    conversation = await repository.get_conversation(
        session, conversation_id=message.conversation_id
    )
    if conversation is None:
        raise TerminalError("INBOX_REPLY_MISSING_CONVERSATION")

    account = await session.get(SocialAccount, conversation.social_account_id)
    if account is None or not account.zernio_account_id or account.credential_id is None:
        raise TerminalError("INBOX_REPLY_MISSING_ACCOUNT")

    if provider is None:
        credential = await get_credential(session, credential_id=account.credential_id)
        if credential is None:
            raise TerminalError("INBOX_REPLY_MISSING_CREDENTIAL")
        provider = get_social_provider_for_credential(
            cfg, alias=credential.alias, secret_ref=credential.secret_ref
        )

    idem_key = message.idempotency_key or f"reply:{message.id}"
    if conversation.kind == "dm":
        result = await provider.send_inbox_message(
            conversation_id=conversation.external_thread_id,
            account_id=account.zernio_account_id,
            message=message.body,
            idempotency_key=idem_key,
        )
    else:
        result = await provider.reply_to_post_comment(
            post_id=conversation.external_thread_id,
            account_id=account.zernio_account_id,
            message=message.body,
            idempotency_key=idem_key,
        )

    await _apply_send_result(session, message=message, result=result)


async def _apply_send_result(
    session: AsyncSession,
    *,
    message: ConversationMessage,
    result: InboxSendResult,
) -> None:
    if result.kind in ("sent", "replayed"):
        await repository.mark_message_sent(
            session,
            message_id=message.id,
            external_message_id=result.external_message_id,
        )
        return
    if result.kind == "in_flight":
        raise RetryableError(after=5.0)
    if result.kind == "conflict":
        log.error(
            "inbox_send_reply.conflict",
            message_id=str(message.id),
            error=result.error_message,
        )
        await repository.mark_message_failed(
            session,
            message_id=message.id,
            error_code="IDEMPOTENCY_CONFLICT",
            error_message=result.error_message,
        )
        raise TerminalError("INBOX_REPLY_CONFLICT")
    if result.kind == "ambiguous":
        await repository.mark_message_outcome_unknown(
            session,
            message_id=message.id,
            error_message=result.error_message,
        )
        return
    # failed
    await repository.mark_message_failed(
        session,
        message_id=message.id,
        error_code="PROVIDER_REJECTED",
        error_message=result.error_message,
    )
    raise TerminalError("INBOX_REPLY_FAILED")


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


async def reconcile_inbox_replies(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
) -> int:
    """Resolve pending/OUTCOME_UNKNOWN outbound by listing provider messages."""
    cfg = settings or get_settings()
    older_than = utc_now() - RECONCILE_PENDING_AGE
    pending = await repository.list_pending_outbound_for_reconcile(
        session, older_than=older_than
    )
    resolved = 0
    for message in pending:
        conversation = await repository.get_conversation(
            session, conversation_id=message.conversation_id
        )
        if conversation is None:
            continue
        account = await session.get(SocialAccount, conversation.social_account_id)
        if account is None or not account.zernio_account_id or account.credential_id is None:
            continue
        credential = await get_credential(session, credential_id=account.credential_id)
        if credential is None:
            continue
        provider = get_social_provider_for_credential(
            cfg, alias=credential.alias, secret_ref=credential.secret_ref
        )

        try:
            found_id = await _find_outbound_on_provider(
                provider,
                conversation=conversation,
                account_id=account.zernio_account_id,
                body=message.body,
            )
        except Exception:
            # Same posture as _sync_post_comments: one bad thread must not kill
            # the whole reconcile cron (Zernio often 400s platform_api_error).
            log.warning(
                "inbox.reconcile_outbound.lookup_failed",
                message_id=str(message.id),
                conversation_id=str(conversation.id),
                external_thread_id=conversation.external_thread_id,
                exc_info=True,
            )
            continue
        if found_id is not None:
            await repository.mark_message_sent(
                session, message_id=message.id, external_message_id=found_id
            )
            resolved += 1
            continue

        # Empty / inconclusive — leave pending unless past hard grace.
        age = utc_now() - message.created_at
        if age >= RECONCILE_FAIL_AFTER and message.error_code == OUTCOME_UNKNOWN:
            # Only fail when we got a non-empty list that clearly lacked the message.
            # Empty list stays pending (inconclusive).
            try:
                listed = await _provider_list_nonempty(
                    provider,
                    conversation=conversation,
                    account_id=account.zernio_account_id,
                )
            except Exception:
                log.warning(
                    "inbox.reconcile_outbound.list_failed",
                    message_id=str(message.id),
                    conversation_id=str(conversation.id),
                    external_thread_id=conversation.external_thread_id,
                    exc_info=True,
                )
                continue
            if listed:
                await repository.mark_message_failed(
                    session,
                    message_id=message.id,
                    error_code="NOT_FOUND_AFTER_GRACE",
                    error_message="outbound not found on provider after grace",
                )
                resolved += 1
    return resolved


async def _find_outbound_on_provider(
    provider: SocialProvider,
    *,
    conversation: Conversation,
    account_id: str,
    body: str,
) -> str | None:
    if conversation.kind == "comment":
        comments = await provider.get_post_comments(
            post_id=conversation.external_thread_id, account_id=account_id
        )
        for c in comments:
            if c.message == body:
                return c.id
        return None

    # DM: list conversations is summary-only; Fake may expose listed outbound via
    # get_post_comments keyed by conversation id (see FakeSocialProvider).
    comments = await provider.get_post_comments(
        post_id=conversation.external_thread_id, account_id=account_id
    )
    for c in comments:
        if c.message == body:
            return c.id
    return None


async def _provider_list_nonempty(
    provider: SocialProvider,
    *,
    conversation: Conversation,
    account_id: str,
) -> bool:
    comments = await provider.get_post_comments(
        post_id=conversation.external_thread_id, account_id=account_id
    )
    return len(comments) > 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _guess_lang(body: str) -> str:
    for ch in body:
        if "\u0600" <= ch <= "\u06ff":
            return "ar"
    return "en"


def _parse_ts(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _inputs_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _transcript_from_rows(
    rows: list[ConversationMessage],
) -> list[reply_prompt.TranscriptMessage]:
    return [
        {
            "direction": row.direction,
            "author_name": row.author_name or "",
            "body": row.body or "",
        }
        for row in rows
    ]


async def _run_structured_validated[T: BaseModel](
    router: LLMTaskRouter,
    task: str,
    request: StructuredGenerationRequest,
    model: type[T],
) -> tuple[T, Any]:
    last_error: ValidationError | None = None
    messages = list(request.messages)
    for attempt in range(2):
        req = request.model_copy(update={"messages": messages})
        response = await router.run_structured(task, req)
        try:
            return model.model_validate(response.content), response
        except ValidationError as exc:
            last_error = exc
            if attempt == 0:
                messages = messages + [
                    LLMMessage(
                        role="user",
                        content=(
                            "Your previous JSON failed validation. "
                            f"Fix these errors and return valid JSON only: {exc.errors()}"
                        ),
                    )
                ]
    assert last_error is not None
    raise last_error


# ---------------------------------------------------------------------------
# HTTP surface (Stage 7)
# ---------------------------------------------------------------------------


def message_to_out(message: ConversationMessage) -> ConversationMessageOut:
    from typing import Literal, cast

    lang = cast(Literal["ar", "en"], message.lang if message.lang in ("ar", "en") else "en")
    direction = cast(
        Literal["inbound", "outbound"],
        message.direction if message.direction in ("inbound", "outbound") else "inbound",
    )
    return ConversationMessageOut(
        id=message.id,
        direction=direction,
        body=message.body,
        author_name=message.author_name,
        author_id=message.author_user_id,
        created_at=message.created_at,
        lang=lang,
    )


async def conversation_to_out(
    session: AsyncSession, conversation: Conversation
) -> ConversationOut:
    from typing import Literal, cast

    preview = await repository.list_recent_messages(
        session, conversation_id=conversation.id
    )
    sentiment = cast(
        Literal["positive", "neutral", "negative"],
        conversation.sentiment
        if conversation.sentiment in ("positive", "neutral", "negative")
        else "neutral",
    )
    status = cast(
        Literal["open", "resolved"],
        conversation.status if conversation.status in ("open", "resolved") else "open",
    )
    return ConversationOut(
        id=conversation.id,
        platform=conversation.platform,
        participant=ConversationParticipantOut(
            name=conversation.participant_name,
            handle=conversation.participant_handle,
            avatar_url=conversation.participant_avatar_url,
        ),
        messages=[message_to_out(m) for m in preview],
        assignee_id=conversation.assignee_user_id,
        tags=list(conversation.tags or []),
        sentiment=sentiment,
        status=status,
        escalated=bool(conversation.escalated),
        post_id=conversation.post_id,
        suggested_reply_ar=conversation.suggested_reply_ar or "",
        suggested_reply_en=conversation.suggested_reply_en or "",
        last_message_at=conversation.last_message_at,
        created_at=conversation.created_at,
    )


def saved_reply_to_out(row: Any) -> SavedReplyOut:
    return SavedReplyOut(
        id=row.id,
        title=row.title,
        body_ar=row.body_ar,
        body_en=row.body_en,
        created_at=row.created_at,
    )


async def list_conversations_page(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    status: str | None = None,
    platform: str | None = None,
    assignee_id: uuid.UUID | None = None,
    unassigned: bool = False,
    tag: str | None = None,
    cursor: str | None = None,
    limit: int = 30,
) -> ConversationPage:
    from app.core.errors import ApiError
    from app.core.pagination import InvalidCursorError

    try:
        rows, next_cursor = await repository.list_conversations(
            session,
            organization_id=organization_id,
            status=status,
            platform=platform,
            assignee_id=assignee_id,
            unassigned=unassigned,
            tag=tag,
            cursor=cursor,
            limit=limit,
        )
    except (InvalidCursorError, KeyError, ValueError, TypeError) as exc:
        raise ApiError("VALIDATION", "Invalid pagination cursor", status_code=422) from exc
    items = [await conversation_to_out(session, row) for row in rows]
    return ConversationPage(items=items, next_cursor=next_cursor)


async def trigger_org_sync(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    skip_classify: bool = True,
    conversation_limit: int = _DEFAULT_MANUAL_CONV_LIMIT,
    mode: Literal["initial", "more"] = "initial",
) -> InboxSyncEnqueueOut:
    account_ids = await enqueue_org_inbox_sync(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        account_id=account_id,
        skip_classify=skip_classify,
        conversation_limit=conversation_limit,
        mode=mode,
    )
    return InboxSyncEnqueueOut(enqueued=len(account_ids), account_ids=account_ids)


async def get_org_sync_status(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> InboxSyncStatusOut:
    rows = await repository.list_sync_states_for_org(
        session, organization_id=organization_id
    )
    accounts: list[InboxSyncAccountStatusOut] = []
    for state, account in rows:
        accounts.append(
            InboxSyncAccountStatusOut(
                account_id=account.id,
                brand_id=account.brand_id,
                platform=account.platform,
                last_synced_at=state.last_synced_at,
                last_sync_status=state.last_sync_status,
                last_error=state.last_error,
                has_more=bool(state.dm_cursor),
            )
        )
    return InboxSyncStatusOut(accounts=accounts)


async def list_messages_page(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 50,
) -> ConversationMessagePage:
    from app.core.errors import ApiError
    from app.core.pagination import InvalidCursorError

    conversation = await repository.get_conversation(
        session, conversation_id=conversation_id
    )
    if conversation is None or conversation.organization_id != organization_id:
        raise ApiError("NOT_FOUND", "Conversation not found", status_code=404)
    try:
        rows, next_cursor = await repository.list_messages(
            session,
            conversation_id=conversation_id,
            cursor=cursor,
            limit=limit,
        )
    except (InvalidCursorError, KeyError, ValueError, TypeError) as exc:
        raise ApiError("VALIDATION", "Invalid pagination cursor", status_code=422) from exc
    return ConversationMessagePage(
        items=[message_to_out(m) for m in rows], next_cursor=next_cursor
    )


def _require_org_conversation(
    conversation: Conversation | None, *, organization_id: uuid.UUID
) -> Conversation:
    from app.core.errors import ApiError

    if conversation is None or conversation.organization_id != organization_id:
        raise ApiError("NOT_FOUND", "Conversation not found", status_code=404)
    return conversation


async def reply_and_return_conversation(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    body: str,
    actor_user_id: uuid.UUID,
    actor_name: str,
) -> ConversationOut:
    from app.features import audit

    conversation = _require_org_conversation(
        await repository.get_conversation(session, conversation_id=conversation_id),
        organization_id=organization_id,
    )
    await send_reply(
        session,
        conversation_id=conversation_id,
        body=body,
        author_user_id=actor_user_id,
        author_name=actor_name,
        lang=_guess_lang(body),
    )
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=conversation.brand_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="inbox.replied",
        target_type="conversation",
        target_id=conversation_id,
    )
    refreshed = _require_org_conversation(
        await repository.get_conversation(session, conversation_id=conversation_id),
        organization_id=organization_id,
    )
    return await conversation_to_out(session, refreshed)


async def assign_conversation(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    assignee_id: uuid.UUID | None,
    actor_user_id: uuid.UUID,
    actor_name: str,
) -> ConversationOut:
    from app.features import audit

    conversation = _require_org_conversation(
        await repository.get_conversation(session, conversation_id=conversation_id),
        organization_id=organization_id,
    )
    updated = await repository.update_assignee(
        session, conversation=conversation, assignee_user_id=assignee_id
    )
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=updated.brand_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="inbox.assigned" if assignee_id else "inbox.unassigned",
        target_type="conversation",
        target_id=conversation_id,
        meta={"assigneeId": str(assignee_id)} if assignee_id else None,
    )
    return await conversation_to_out(session, updated)


async def set_conversation_tags(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    tags: list[str],
    actor_user_id: uuid.UUID,
    actor_name: str,
) -> ConversationOut:
    from app.features import audit

    conversation = _require_org_conversation(
        await repository.get_conversation(session, conversation_id=conversation_id),
        organization_id=organization_id,
    )
    updated = await repository.update_tags(
        session, conversation=conversation, tags=tags
    )
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=updated.brand_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="inbox.tags_updated",
        target_type="conversation",
        target_id=conversation_id,
        meta={"tags": tags},
    )
    return await conversation_to_out(session, updated)


async def resolve_conversation(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_name: str,
) -> ConversationOut:
    from app.features import audit

    conversation = _require_org_conversation(
        await repository.get_conversation(session, conversation_id=conversation_id),
        organization_id=organization_id,
    )
    updated = await repository.resolve_conversation(
        session, conversation=conversation
    )
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=updated.brand_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="inbox.resolved",
        target_type="conversation",
        target_id=conversation_id,
    )
    return await conversation_to_out(session, updated)


async def reopen_conversation(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_name: str,
) -> ConversationOut:
    from app.features import audit

    conversation = _require_org_conversation(
        await repository.get_conversation(session, conversation_id=conversation_id),
        organization_id=organization_id,
    )
    updated = await repository.reopen_conversation(session, conversation=conversation)
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=updated.brand_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="inbox.reopened",
        target_type="conversation",
        target_id=conversation_id,
    )
    return await conversation_to_out(session, updated)


async def escalate_conversation(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_name: str,
) -> ConversationOut:
    from app.features import audit

    conversation = _require_org_conversation(
        await repository.get_conversation(session, conversation_id=conversation_id),
        organization_id=organization_id,
    )
    updated = await repository.escalate_conversation(
        session, conversation=conversation
    )
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=updated.brand_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="inbox.escalated",
        target_type="conversation",
        target_id=conversation_id,
    )
    return await conversation_to_out(session, updated)


async def list_saved_replies_out(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> list[SavedReplyOut]:
    rows = await repository.list_saved_replies(session, organization_id=organization_id)
    return [saved_reply_to_out(row) for row in rows]


async def create_saved_reply_out(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    title: str,
    body_ar: str,
    body_en: str,
    actor_user_id: uuid.UUID,
    actor_name: str,
) -> SavedReplyOut:
    from app.features import audit

    row = await repository.create_saved_reply(
        session,
        organization_id=organization_id,
        title=title,
        body_ar=body_ar,
        body_en=body_en,
        created_by=actor_user_id,
    )
    await audit.record(
        session,
        organization_id=organization_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="inbox.saved_reply_created",
        target_type="saved_reply",
        target_id=row.id,
    )
    return saved_reply_to_out(row)


async def delete_saved_reply_out(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    saved_reply_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_name: str,
) -> None:
    from app.core.errors import ApiError
    from app.features import audit

    deleted = await repository.soft_delete_saved_reply(
        session, organization_id=organization_id, saved_reply_id=saved_reply_id
    )
    if not deleted:
        raise ApiError("NOT_FOUND", "Saved reply not found", status_code=404)
    await audit.record(
        session,
        organization_id=organization_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="inbox.saved_reply_deleted",
        target_type="saved_reply",
        target_id=saved_reply_id,
    )
