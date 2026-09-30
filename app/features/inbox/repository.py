"""Inbox persistence — conversations, messages, saved replies."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.features.inbox.models import (
    Conversation,
    ConversationMessage,
    InboxSyncState,
    SavedReply,
)

if TYPE_CHECKING:
    from app.features.social_accounts import SocialAccount


async def upsert_conversation(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    social_account_id: uuid.UUID,
    platform: str,
    kind: str,
    external_thread_id: str,
    participant_name: str,
    participant_handle: str | None = None,
    participant_avatar_url: str | None = None,
    participant_external_id: str | None = None,
    post_id: uuid.UUID | None = None,
    last_message_at: datetime | None = None,
    last_inbound_at: datetime | None = None,
) -> Conversation:
    """Insert or update conversation on ux_conv_external."""
    now = utc_now()
    msg_at = last_message_at or now
    inbound_at = last_inbound_at or msg_at
    stmt = (
        insert(Conversation)
        .values(
            organization_id=organization_id,
            brand_id=brand_id,
            social_account_id=social_account_id,
            platform=platform,
            kind=kind,
            external_thread_id=external_thread_id,
            participant_name=participant_name,
            participant_handle=participant_handle,
            participant_avatar_url=participant_avatar_url,
            participant_external_id=participant_external_id,
            post_id=post_id,
            last_message_at=msg_at,
            last_inbound_at=inbound_at,
            message_count=0,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_update(
            index_elements=["social_account_id", "external_thread_id"],
            set_={
                "participant_name": sa.text("EXCLUDED.participant_name"),
                "participant_handle": sa.text("EXCLUDED.participant_handle"),
                "participant_avatar_url": sa.text("EXCLUDED.participant_avatar_url"),
                "participant_external_id": sa.text("EXCLUDED.participant_external_id"),
                "post_id": sa.text("COALESCE(EXCLUDED.post_id, conversations.post_id)"),
                "last_message_at": sa.text(
                    "GREATEST(conversations.last_message_at, EXCLUDED.last_message_at)"
                ),
                "last_inbound_at": sa.text(
                    "GREATEST(conversations.last_inbound_at, EXCLUDED.last_inbound_at)"
                ),
                "updated_at": sa.text("EXCLUDED.updated_at"),
            },
        )
        .returning(Conversation)
    )
    result = await session.execute(stmt)
    row = result.scalar_one()
    await session.flush()
    return row


async def insert_inbound_message(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    body: str,
    lang: str,
    author_name: str,
    external_message_id: str | None,
) -> ConversationMessage | None:
    """Insert inbound message; return None on external_message_id conflict."""
    if external_message_id is not None:
        stmt = (
            insert(ConversationMessage)
            .values(
                organization_id=organization_id,
                conversation_id=conversation_id,
                direction="inbound",
                body=body,
                lang=lang,
                author_name=author_name,
                external_message_id=external_message_id,
                delivery_status="received",
            )
            .on_conflict_do_nothing(
                index_elements=["conversation_id", "external_message_id"],
                index_where=sa.text("external_message_id IS NOT NULL"),
            )
            .returning(ConversationMessage)
        )
        result = await session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        await _bump_conversation_counts(
            session, conversation_id=conversation_id, inbound=True
        )
        await session.flush()
        return row

    msg = ConversationMessage(
        organization_id=organization_id,
        conversation_id=conversation_id,
        direction="inbound",
        body=body,
        lang=lang,
        author_name=author_name,
        external_message_id=None,
        delivery_status="received",
    )
    session.add(msg)
    await session.flush()
    await _bump_conversation_counts(
        session, conversation_id=conversation_id, inbound=True
    )
    return msg


async def insert_synced_message(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    direction: str,
    body: str,
    lang: str,
    author_name: str,
    external_message_id: str,
    created_at: datetime | None = None,
) -> ConversationMessage | None:
    """Insert a historical inbound/outbound message from provider backfill.

    Returns None when ``external_message_id`` already exists for the thread.
    """
    now = utc_now()
    msg_at = created_at or now
    delivery = "received" if direction == "inbound" else "sent"
    values: dict[str, Any] = {
        "organization_id": organization_id,
        "conversation_id": conversation_id,
        "direction": direction,
        "body": body,
        "lang": lang,
        "author_name": author_name,
        "external_message_id": external_message_id,
        "delivery_status": delivery,
        "created_at": msg_at,
    }
    if direction == "outbound":
        values["sent_at"] = msg_at
    stmt = (
        insert(ConversationMessage)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=["conversation_id", "external_message_id"],
            index_where=sa.text("external_message_id IS NOT NULL"),
        )
        .returning(ConversationMessage)
    )
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()
    if row is None:
        return None
    await _bump_conversation_counts(
        session,
        conversation_id=conversation_id,
        inbound=(direction == "inbound"),
        at=msg_at,
    )
    await session.flush()
    return row


async def insert_outbound_pending(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    conversation_id: uuid.UUID,
    body: str,
    lang: str,
    author_user_id: uuid.UUID | None,
    author_name: str,
    message_id: uuid.UUID | None = None,
) -> ConversationMessage:
    from app.core.ids import new_uuid7

    mid = message_id or new_uuid7()
    msg = ConversationMessage(
        id=mid,
        organization_id=organization_id,
        conversation_id=conversation_id,
        direction="outbound",
        body=body,
        lang=lang,
        author_user_id=author_user_id,
        author_name=author_name,
        idempotency_key=f"reply:{mid}",
        delivery_status="pending",
    )
    session.add(msg)
    await session.flush()
    await _bump_conversation_counts(
        session, conversation_id=conversation_id, inbound=False
    )
    return msg


async def _bump_conversation_counts(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    inbound: bool,
    at: datetime | None = None,
) -> None:
    # Atomic increment — concurrent webhook/sync inserts must not race on
    # a read-modify-write of message_count.
    stamp = at or utc_now()
    now = utc_now()
    values: dict[str, Any] = {
        "message_count": Conversation.message_count + 1,
        "updated_at": now,
        "last_message_at": sa.case(
            (
                sa.or_(
                    Conversation.last_message_at.is_(None),
                    Conversation.last_message_at < stamp,
                ),
                stamp,
            ),
            else_=Conversation.last_message_at,
        ),
    }
    if inbound:
        values["last_inbound_at"] = sa.case(
            (
                sa.or_(
                    Conversation.last_inbound_at.is_(None),
                    Conversation.last_inbound_at < stamp,
                ),
                stamp,
            ),
            else_=Conversation.last_inbound_at,
        )
    await session.execute(
        sa.update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(**values)
    )
    # Complex CASE expressions expire the identity-map row; reload so async
    # callers that still hold the Conversation instance can read attributes.
    await session.get(Conversation, conversation_id, populate_existing=True)


async def get_conversation(
    session: AsyncSession, *, conversation_id: uuid.UUID
) -> Conversation | None:
    return await session.get(Conversation, conversation_id)


async def get_conversation_by_external(
    session: AsyncSession,
    *,
    social_account_id: uuid.UUID,
    external_thread_id: str,
) -> Conversation | None:
    stmt = sa.select(Conversation).where(
        Conversation.social_account_id == social_account_id,
        Conversation.external_thread_id == external_thread_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_message(
    session: AsyncSession, *, message_id: uuid.UUID
) -> ConversationMessage | None:
    return await session.get(ConversationMessage, message_id)


async def get_message_by_external_id(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    external_message_id: str,
) -> ConversationMessage | None:
    stmt = sa.select(ConversationMessage).where(
        ConversationMessage.conversation_id == conversation_id,
        ConversationMessage.external_message_id == external_message_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_message_by_external_id(
    session: AsyncSession, *, external_message_id: str
) -> ConversationMessage | None:
    stmt = (
        sa.select(ConversationMessage)
        .where(ConversationMessage.external_message_id == external_message_id)
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def update_outbound_delivery_status(
    session: AsyncSession,
    *,
    message: ConversationMessage,
    delivery_status: str,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    message.delivery_status = delivery_status
    if delivery_status == "sent" and message.sent_at is None:
        message.sent_at = utc_now()
    if delivery_status == "failed":
        message.error_code = error_code or "PROVIDER_FAILED"
        message.error_message = error_message
    elif delivery_status == "sent":
        message.error_code = None
        message.error_message = None
    await session.flush()


async def update_conversation_classification(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    sentiment: str,
    sentiment_decision_id: uuid.UUID,
    suggested_reply_ar: str,
    suggested_reply_en: str,
    suggestion_decision_id: uuid.UUID,
) -> None:
    conv = await session.get(Conversation, conversation_id)
    if conv is None:
        return
    conv.sentiment = sentiment
    conv.sentiment_decision_id = sentiment_decision_id
    conv.suggested_reply_ar = suggested_reply_ar
    conv.suggested_reply_en = suggested_reply_en
    conv.suggestion_decision_id = suggestion_decision_id
    conv.updated_at = utc_now()
    await session.flush()


async def mark_message_sent(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    external_message_id: str | None,
) -> None:
    msg = await session.get(ConversationMessage, message_id)
    if msg is None:
        return
    msg.delivery_status = "sent"
    msg.external_message_id = external_message_id
    msg.error_code = None
    msg.error_message = None
    msg.sent_at = utc_now()
    await session.flush()


async def mark_message_failed(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    error_code: str,
    error_message: str | None = None,
) -> None:
    msg = await session.get(ConversationMessage, message_id)
    if msg is None:
        return
    msg.delivery_status = "failed"
    msg.error_code = error_code
    msg.error_message = error_message
    await session.flush()


async def mark_message_outcome_unknown(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    error_message: str | None = None,
) -> None:
    msg = await session.get(ConversationMessage, message_id)
    if msg is None:
        return
    msg.delivery_status = "pending"
    msg.error_code = "OUTCOME_UNKNOWN"
    msg.error_message = error_message
    await session.flush()


async def list_pending_outbound_for_reconcile(
    session: AsyncSession,
    *,
    older_than: datetime,
) -> list[ConversationMessage]:
    """Pending outbound with OUTCOME_UNKNOWN or pending older than threshold."""
    stmt = (
        sa.select(ConversationMessage)
        .where(
            ConversationMessage.direction == "outbound",
            ConversationMessage.delivery_status == "pending",
            sa.or_(
                ConversationMessage.error_code == "OUTCOME_UNKNOWN",
                ConversationMessage.created_at < older_than,
            ),
        )
        .order_by(ConversationMessage.created_at)
        .limit(100)
    )
    return list((await session.execute(stmt)).scalars().all())


DEFAULT_CONVERSATION_PAGE_SIZE = 30
MAX_CONVERSATION_PAGE_SIZE = 100
DEFAULT_MESSAGE_PAGE_SIZE = 50
MAX_MESSAGE_PAGE_SIZE = 100
MESSAGE_PREVIEW_LIMIT = 10


async def list_conversations(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    status: str | None = None,
    platform: str | None = None,
    assignee_id: uuid.UUID | None = None,
    unassigned: bool = False,
    tag: str | None = None,
    cursor: str | None = None,
    limit: int = DEFAULT_CONVERSATION_PAGE_SIZE,
) -> tuple[list[Conversation], str | None]:
    from app.core.pagination import decode_cursor, encode_cursor

    limit = min(max(limit, 1), MAX_CONVERSATION_PAGE_SIZE)
    stmt = sa.select(Conversation).where(
        Conversation.organization_id == organization_id
    )
    if status is not None:
        stmt = stmt.where(Conversation.status == status)
    if platform is not None:
        stmt = stmt.where(Conversation.platform == platform)
    if unassigned:
        stmt = stmt.where(Conversation.assignee_user_id.is_(None))
    elif assignee_id is not None:
        stmt = stmt.where(Conversation.assignee_user_id == assignee_id)
    if tag is not None:
        stmt = stmt.where(Conversation.tags.contains([tag]))
    if cursor is not None:
        parsed = decode_cursor(cursor)
        cursor_at = datetime.fromisoformat(str(parsed["last_message_at"]))
        cursor_id = uuid.UUID(str(parsed["id"]))
        stmt = stmt.where(
            sa.tuple_(Conversation.last_message_at, Conversation.id)
            < (cursor_at, cursor_id)
        )
    stmt = stmt.order_by(
        Conversation.last_message_at.desc(), Conversation.id.desc()
    ).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(
            {
                "last_message_at": last.last_message_at.isoformat(),
                "id": str(last.id),
            }
        )
    return page, next_cursor


async def list_messages(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = DEFAULT_MESSAGE_PAGE_SIZE,
) -> tuple[list[ConversationMessage], str | None]:
    """Ascending (created_at, id) — oldest first (architecture/02 §16)."""
    from app.core.pagination import decode_cursor, encode_cursor

    limit = min(max(limit, 1), MAX_MESSAGE_PAGE_SIZE)
    stmt = sa.select(ConversationMessage).where(
        ConversationMessage.conversation_id == conversation_id
    )
    if cursor is not None:
        parsed = decode_cursor(cursor)
        cursor_at = datetime.fromisoformat(str(parsed["created_at"]))
        cursor_id = uuid.UUID(str(parsed["id"]))
        stmt = stmt.where(
            sa.tuple_(ConversationMessage.created_at, ConversationMessage.id)
            > (cursor_at, cursor_id)
        )
    stmt = stmt.order_by(
        ConversationMessage.created_at.asc(), ConversationMessage.id.asc()
    ).limit(limit + 1)
    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(
            {"created_at": last.created_at.isoformat(), "id": str(last.id)}
        )
    return page, next_cursor


async def list_recent_messages(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    limit: int = MESSAGE_PREVIEW_LIMIT,
) -> list[ConversationMessage]:
    """Most recent N messages, returned in ascending order for preview display."""
    stmt = (
        sa.select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conversation_id)
        .order_by(
            ConversationMessage.created_at.desc(), ConversationMessage.id.desc()
        )
        .limit(limit)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    rows.reverse()
    return rows


async def update_assignee(
    session: AsyncSession,
    *,
    conversation: Conversation,
    assignee_user_id: uuid.UUID | None,
) -> Conversation:
    conversation.assignee_user_id = assignee_user_id
    conversation.updated_at = utc_now()
    await session.flush()
    return conversation


async def update_tags(
    session: AsyncSession, *, conversation: Conversation, tags: list[str]
) -> Conversation:
    conversation.tags = list(tags)
    conversation.updated_at = utc_now()
    await session.flush()
    return conversation


async def resolve_conversation(
    session: AsyncSession, *, conversation: Conversation
) -> Conversation:
    conversation.status = "resolved"
    conversation.resolved_at = utc_now()
    conversation.updated_at = utc_now()
    await session.flush()
    return conversation


async def reopen_conversation(
    session: AsyncSession, *, conversation: Conversation
) -> Conversation:
    conversation.status = "open"
    conversation.resolved_at = None
    conversation.updated_at = utc_now()
    await session.flush()
    return conversation


async def escalate_conversation(
    session: AsyncSession, *, conversation: Conversation
) -> Conversation:
    conversation.escalated = True
    conversation.escalated_at = utc_now()
    conversation.updated_at = utc_now()
    await session.flush()
    return conversation


async def get_saved_reply(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    saved_reply_id: uuid.UUID,
) -> SavedReply | None:
    stmt = sa.select(SavedReply).where(
        SavedReply.id == saved_reply_id,
        SavedReply.organization_id == organization_id,
        SavedReply.deleted_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


# --- saved replies ---------------------------------------------------------


async def list_saved_replies(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> list[SavedReply]:
    stmt = (
        sa.select(SavedReply)
        .where(
            SavedReply.organization_id == organization_id,
            SavedReply.deleted_at.is_(None),
        )
        .order_by(SavedReply.title)
    )
    return list((await session.execute(stmt)).scalars().all())


async def create_saved_reply(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    title: str,
    body_ar: str,
    body_en: str,
    created_by: uuid.UUID | None,
) -> SavedReply:
    row = SavedReply(
        organization_id=organization_id,
        title=title,
        body_ar=body_ar,
        body_en=body_en,
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    return row


async def soft_delete_saved_reply(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    saved_reply_id: uuid.UUID,
) -> bool:
    from sqlalchemy.engine import CursorResult

    result: CursorResult[Any] = await session.execute(  # type: ignore[assignment]
        sa.update(SavedReply)
        .where(
            SavedReply.id == saved_reply_id,
            SavedReply.organization_id == organization_id,
            SavedReply.deleted_at.is_(None),
        )
        .values(deleted_at=utc_now())
    )
    return (result.rowcount or 0) > 0


def pending_grace_cutoff(*, grace_seconds: float = 60.0) -> datetime:
    return utc_now() - timedelta(seconds=grace_seconds)


# --- inbox sync state ------------------------------------------------------


async def get_or_create_sync_state(
    session: AsyncSession, *, social_account_id: uuid.UUID
) -> InboxSyncState:
    row = await session.get(InboxSyncState, social_account_id)
    if row is not None:
        return row
    stmt = (
        insert(InboxSyncState)
        .values(social_account_id=social_account_id)
        .on_conflict_do_nothing(index_elements=["social_account_id"])
        .returning(InboxSyncState.social_account_id)
    )
    await session.execute(stmt)
    await session.flush()
    row = await session.get(InboxSyncState, social_account_id)
    if row is None:
        raise RuntimeError(f"inbox_sync_state missing for {social_account_id}")
    return row


async def mark_sync_ok(
    session: AsyncSession,
    *,
    state: InboxSyncState,
    dm_cursor: str | None = None,
) -> None:
    state.last_synced_at = utc_now()
    state.last_sync_status = "ok"
    state.last_error = None
    state.consecutive_failures = 0
    state.updated_at = utc_now()
    if dm_cursor is not None:
        state.dm_cursor = dm_cursor
    await session.flush()


async def mark_sync_error(
    session: AsyncSession, *, state: InboxSyncState, error: str
) -> None:
    state.last_sync_status = "error"
    state.last_error = error[:2000]
    state.consecutive_failures = int(state.consecutive_failures or 0) + 1
    state.updated_at = utc_now()
    await session.flush()


async def list_sync_states_for_org(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> list[tuple[InboxSyncState, SocialAccount]]:
    """Return (state, social_account) for accounts in the org that have sync state."""
    from app.features.social_accounts import SocialAccount

    stmt = (
        sa.select(InboxSyncState, SocialAccount)
        .join(
            SocialAccount,
            SocialAccount.id == InboxSyncState.social_account_id,
        )
        .where(SocialAccount.organization_id == organization_id)
        .order_by(SocialAccount.platform)
    )
    rows = await session.execute(stmt)
    return [(state, account) for state, account in rows.all()]


async def list_recent_published_posts(
    session: AsyncSession,
    *,
    brand_id: uuid.UUID,
    platform: str,
    since: datetime,
    limit: int,
) -> list[Any]:
    from app.features.posts import Post

    stmt = (
        sa.select(Post)
        .where(
            Post.brand_id == brand_id,
            Post.platform == platform,
            Post.status == "published",
            Post.zernio_post_id.is_not(None),
            Post.deleted_at.is_(None),
            Post.published_at.is_not(None),
            Post.published_at >= since,
        )
        .order_by(Post.published_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())
