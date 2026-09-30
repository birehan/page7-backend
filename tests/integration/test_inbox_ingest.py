"""Phase 12 Stage 5 — inbox webhook ingest + classify."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.content_ai.models import AiDecision
from app.features.inbox.models import Conversation, ConversationMessage
from app.features.social_accounts.models import SocialAccount, WebhookEvent
from app.features.social_accounts.tasks import process_webhook_event
from app.integrations.social import reset_fake_social_provider
from app.jobs.handlers.inbox import handle_inbox_classify_message
from tests.db_fixtures import SeedMember

_WEBHOOK_SECRET = "test-webhook-secret"  # noqa: S105


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Inbox Brand {uuid.uuid4().hex[:6]}",
            "industry": "retail",
            "city": "Riyadh",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def _connect_instagram(
    client: AsyncClient, org_id: uuid.UUID, brand_id: str
) -> dict[str, Any]:
    oauth = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/channels/instagram/oauth-url"
    )
    assert oauth.status_code == 200, oauth.text
    channels = (
        await client.get(f"/v1/orgs/{org_id}/brands/{brand_id}/channels")
    ).json()
    ig = next(c for c in channels if c["platform"] == "instagram")
    connected = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/channels/{ig['id']}/connect"
    )
    assert connected.status_code == 200, connected.text
    return connected.json()  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZERNIO_WEBHOOK_SECRET__t1", _WEBHOOK_SECRET)
    monkeypatch.setenv("ZERNIO_API_KEY__t1", "fake-key-t1")
    monkeypatch.setenv("ZERNIO_API_KEY__t2", "fake-key-t2")
    monkeypatch.setenv("ZERNIO_API_KEY__t3", "fake-key-t3")
    monkeypatch.setenv("LLM__PROVIDER", "fake")
    reset_fake_social_provider()


async def _insert_webhook(
    db_session: AsyncSession,
    *,
    event_type: str,
    payload: dict[str, Any],
    external_event_id: str | None = None,
) -> WebhookEvent:
    event = WebhookEvent(
        provider="zernio",
        external_event_id=external_event_id or f"evt_{uuid.uuid4().hex[:12]}",
        event_type=event_type,
        payload=payload,
        signature_valid=True,
    )
    db_session.add(event)
    await db_session.flush()
    return event


@pytest.mark.asyncio
async def test_comment_received_upserts_and_classify(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    channel = await _connect_instagram(client, org_id, brand["id"])

    await db_session.rollback()
    account = (
        await db_session.execute(
            select(SocialAccount).where(SocialAccount.id == uuid.UUID(channel["id"]))
        )
    ).scalar_one()
    assert account.zernio_account_id

    payload = {
        "id": f"evt_cmt_{uuid.uuid4().hex[:8]}",
        "type": "comment.received",
        "data": {
            "accountId": account.zernio_account_id,
            "postId": "zp_post_inbox_1",
            "commentId": f"cmt_{uuid.uuid4().hex[:8]}",
            "message": "Love this product!",
            "authorName": "Sara",
            "authorHandle": "@sara",
            "platform": "instagram",
        },
    }
    event = await _insert_webhook(
        db_session,
        event_type="comment.received",
        payload=payload,
        external_event_id=str(payload["id"]),
    )
    await db_session.commit()

    await process_webhook_event(db_session, webhook_event_id=event.id, alias="t1")
    await db_session.commit()
    db_session.expire_all()

    convs = (
        await db_session.execute(
            select(Conversation).where(Conversation.organization_id == org_id)
        )
    ).scalars().all()
    assert len(convs) == 1
    assert convs[0].kind == "comment"
    assert convs[0].external_thread_id == "zp_post_inbox_1"
    conversation_id = convs[0].id

    msgs = (
        await db_session.execute(
            select(ConversationMessage).where(
                ConversationMessage.conversation_id == conversation_id
            )
        )
    ).scalars().all()
    assert len(msgs) == 1
    assert msgs[0].body == "Love this product!"
    assert msgs[0].direction == "inbound"
    message_id = msgs[0].id

    classify_jobs = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM jobs "
                "WHERE type = 'inbox_classify_message' "
                "AND payload->>'message_id' = :mid"
            ),
            {"mid": str(message_id)},
        )
    ).scalar_one()
    assert int(classify_jobs) == 1

    await handle_inbox_classify_message({"message_id": str(message_id)})
    db_session.expire_all()

    conv = (
        await db_session.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        )
    ).scalar_one()
    assert conv.sentiment == "positive"
    assert conv.sentiment_decision_id is not None
    assert conv.suggestion_decision_id is not None
    assert conv.suggested_reply_ar
    assert conv.suggested_reply_en

    decisions = (
        await db_session.execute(
            select(AiDecision).where(
                AiDecision.organization_id == org_id,
                AiDecision.kind.in_(("sentiment", "reply_suggest")),
            )
        )
    ).scalars().all()
    kinds = {d.kind for d in decisions}
    assert kinds == {"sentiment", "reply_suggest"}


@pytest.mark.asyncio
async def test_live_nested_comment_received_shape(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    """Zernio's live comment.received nests post/comment/account (not data{})."""
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    channel = await _connect_instagram(client, org_id, brand["id"])

    await db_session.rollback()
    account = (
        await db_session.execute(
            select(SocialAccount).where(SocialAccount.id == uuid.UUID(channel["id"]))
        )
    ).scalar_one()
    assert account.zernio_account_id
    zernio_account_id = account.zernio_account_id

    own_payload = {
        "id": f"evt_own_{uuid.uuid4().hex[:8]}",
        "event": "comment.received",
        "account": {
            "id": zernio_account_id,
            "platform": "instagram",
        },
        "comment": {
            "id": f"cmt_own_{uuid.uuid4().hex[:8]}",
            "text": "our own reply",
            "author": {"id": "page_1", "name": "Us", "isOwnAccount": True},
            "postId": "zp_live_post_1",
        },
        "post": {"id": "zp_live_post_1"},
    }
    own_event = await _insert_webhook(
        db_session,
        event_type="comment.received",
        payload=own_payload,
        external_event_id=str(own_payload["id"]),
    )
    await db_session.commit()
    await process_webhook_event(db_session, webhook_event_id=own_event.id, alias="t1")
    await db_session.commit()
    db_session.expire_all()
    assert (
        await db_session.execute(
            select(Conversation).where(Conversation.organization_id == org_id)
        )
    ).scalars().all() == []

    visitor_payload = {
        "id": f"evt_vis_{uuid.uuid4().hex[:8]}",
        "event": "comment.received",
        "account": {
            "id": zernio_account_id,
            "platform": "instagram",
        },
        "comment": {
            "id": f"cmt_vis_{uuid.uuid4().hex[:8]}",
            "text": "What are your hours?",
            "author": {"id": "visitor_1", "name": "Visitor", "isOwnAccount": False},
            "postId": "zp_live_post_1",
            "platform": "instagram",
        },
        "post": {"id": "zp_live_post_1"},
    }
    vis_event = await _insert_webhook(
        db_session,
        event_type="comment.received",
        payload=visitor_payload,
        external_event_id=str(visitor_payload["id"]),
    )
    await db_session.commit()
    await process_webhook_event(db_session, webhook_event_id=vis_event.id, alias="t1")
    await db_session.commit()
    db_session.expire_all()

    convs = (
        await db_session.execute(
            select(Conversation).where(Conversation.organization_id == org_id)
        )
    ).scalars().all()
    assert len(convs) == 1
    assert convs[0].external_thread_id == "zp_live_post_1"
    assert convs[0].participant_name == "Visitor"
    msgs = (
        await db_session.execute(
            select(ConversationMessage).where(
                ConversationMessage.conversation_id == convs[0].id
            )
        )
    ).scalars().all()
    assert len(msgs) == 1
    assert msgs[0].body == "What are your hours?"


@pytest.mark.asyncio
async def test_duplicate_webhook_delivery_noop(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    channel = await _connect_instagram(client, org_id, brand["id"])

    await db_session.rollback()
    account = (
        await db_session.execute(
            select(SocialAccount).where(SocialAccount.id == uuid.UUID(channel["id"]))
        )
    ).scalar_one()

    external_id = f"evt_dedupe_inbox_{uuid.uuid4().hex[:8]}"
    payload = {
        "id": external_id,
        "type": "comment.received",
        "data": {
            "accountId": account.zernio_account_id,
            "postId": "zp_dedupe_1",
            "commentId": "cmt_dedupe_1",
            "message": "hello once",
            "authorName": "Ada",
        },
    }
    body = json.dumps(payload).encode()
    sig = hmac.new(_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Zernio-Signature": sig,
    }

    app = __import__("app.main", fromlist=["create_app"]).create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://test") as bare:
        first = await bare.post("/v1/webhooks/zernio/t1", content=body, headers=headers)
        second = await bare.post("/v1/webhooks/zernio/t1", content=body, headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    await db_session.rollback()
    events = (
        await db_session.execute(
            select(WebhookEvent).where(WebhookEvent.external_event_id == external_id)
        )
    ).scalars().all()
    assert len(events) == 1

    jobs = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM jobs "
                "WHERE type = 'process_webhook_event' "
                "AND payload->>'webhook_event_id' = :eid"
            ),
            {"eid": str(events[0].id)},
        )
    ).scalar_one()
    assert int(jobs) == 1


@pytest.mark.asyncio
async def test_analytics_synced_enqueues_sync(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    channel = await _connect_instagram(client, org_id, brand["id"])

    await db_session.rollback()
    account = (
        await db_session.execute(
            select(SocialAccount).where(SocialAccount.id == uuid.UUID(channel["id"]))
        )
    ).scalar_one()
    assert account.credential_id is not None
    credential_id = account.credential_id

    payload = {
        "id": f"evt_as_{uuid.uuid4().hex[:8]}",
        "type": "analytics.synced",
        "data": {"accountId": account.zernio_account_id},
    }
    event = await _insert_webhook(
        db_session,
        event_type="analytics.synced",
        payload=payload,
        external_event_id=str(payload["id"]),
    )
    await db_session.commit()

    await process_webhook_event(db_session, webhook_event_id=event.id, alias="t1")
    await db_session.commit()
    db_session.expire_all()

    jobs = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM jobs "
                "WHERE type = 'analytics_sync' "
                "AND unique_key = :uk "
                "AND state IN ('queued', 'running')"
            ),
            {"uk": f"analytics_sync_wh:{credential_id}"},
        )
    ).scalar_one()
    assert int(jobs) == 1
