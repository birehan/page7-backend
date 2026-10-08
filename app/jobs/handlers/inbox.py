"""Job handlers for inbox (Phase 12 Stages 5–6)."""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.features import inbox as inbox_feature
from app.integrations.llm import get_llm_router
from app.jobs.errors import TerminalError

log = structlog.get_logger(__name__)


async def handle_inbox_sync(payload: dict[str, Any]) -> None:
    raw = payload.get("social_account_id") or payload.get("account_id")
    if not isinstance(raw, str):
        raise TerminalError("INBOX_SYNC_BAD_PAYLOAD")
    try:
        social_account_id = uuid.UUID(raw)
    except ValueError as exc:
        raise TerminalError("INBOX_SYNC_BAD_PAYLOAD") from exc

    skip_classify = payload.get("skip_classify", True)
    if not isinstance(skip_classify, bool):
        skip_classify = True

    mode_raw = payload.get("mode", "poll")
    mode = mode_raw if mode_raw in ("initial", "more", "poll") else "poll"

    conversation_limit: int | None = None
    limit_raw = payload.get("conversation_limit")
    if isinstance(limit_raw, int) and limit_raw > 0:
        conversation_limit = limit_raw

    factory = get_session_factory()
    async with factory() as session:
        count = await inbox_feature.sync_backfill(
            session,
            social_account_id=social_account_id,
            skip_classify=skip_classify,
            conversation_limit=conversation_limit,
            mode=mode,  # type: ignore[arg-type]
        )
        await session.commit()
    log.info(
        "inbox_sync.done",
        social_account_id=raw,
        upserted=count,
        mode=mode,
        skip_classify=skip_classify,
    )


async def handle_inbox_poll(payload: dict[str, Any]) -> None:
    _ = payload
    factory = get_session_factory()
    async with factory() as session:
        count = await inbox_feature.enqueue_inbox_poll(session)
        await session.commit()
    log.info("inbox_poll.done", enqueued=count)


async def handle_inbox_classify_message(payload: dict[str, Any]) -> None:
    raw = payload.get("message_id")
    if not isinstance(raw, str):
        raise TerminalError("INBOX_CLASSIFY_BAD_PAYLOAD")
    try:
        message_id = uuid.UUID(raw)
    except ValueError as exc:
        raise TerminalError("INBOX_CLASSIFY_BAD_PAYLOAD") from exc

    settings = get_settings()
    router = get_llm_router(settings)
    factory = get_session_factory()
    async with factory() as session:
        await inbox_feature.classify_message(
            session, message_id=message_id, settings=settings, router=router
        )
        await session.commit()


async def handle_inbox_send_reply(payload: dict[str, Any]) -> None:
    raw = payload.get("message_id")
    if not isinstance(raw, str):
        raise TerminalError("INBOX_SEND_REPLY_BAD_PAYLOAD")
    try:
        message_id = uuid.UUID(raw)
    except ValueError as exc:
        raise TerminalError("INBOX_SEND_REPLY_BAD_PAYLOAD") from exc

    factory = get_session_factory()
    async with factory() as session:
        await inbox_feature.deliver_outbound_reply(session, message_id=message_id)
        await session.commit()


async def handle_reconcile_inbox_replies(payload: dict[str, Any]) -> None:
    _ = payload
    factory = get_session_factory()
    async with factory() as session:
        count = await inbox_feature.reconcile_inbox_replies(session)
        await session.commit()
    log.info("reconcile_inbox_replies.done", resolved=count)
