"""Inbox AI draft — last-20 message context via POST .../ai-draft.

Callers: pytest integration suite. Covers AiDraftOut
(suggestedReplyAr/En) for POST /v1/orgs/{orgId}/inbox/conversations/{id}/ai-draft.
User: Implement the plan as specified, it is attached for your reference.
Do NOT edit the plan file itself.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import utc_now
from app.features.inbox import models as inbox_models
from app.features.inbox import repository as inbox_repo
from app.features.inbox.prompts import reply_suggestion as reply_prompt
from app.features.inbox.service import REPLY_DRAFT_MESSAGE_LIMIT
from app.features.social_accounts.models import SocialAccount
from app.integrations.social import reset_fake_social_provider
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Draft Brand {uuid.uuid4().hex[:6]}",
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


async def _seed_dm_conversation(
    db_session: AsyncSession,
    *,
    org_id: uuid.UUID,
    brand_id: uuid.UUID,
    account: SocialAccount,
) -> inbox_models.Conversation:
    return await inbox_repo.upsert_conversation(
        db_session,
        organization_id=org_id,
        brand_id=brand_id,
        social_account_id=account.id,
        platform="instagram",
        kind="dm",
        external_thread_id=f"conv_{uuid.uuid4().hex[:8]}",
        participant_name="Ada",
        participant_handle="@ada",
    )


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZERNIO_API_KEY__t1", "fake-key-t1")
    monkeypatch.setenv("ZERNIO_API_KEY__t2", "fake-key-t2")
    monkeypatch.setenv("ZERNIO_API_KEY__t3", "fake-key-t3")
    reset_fake_social_provider()


def test_reply_prompt_includes_transcript() -> None:
    messages = reply_prompt.build(
        messages=[
            {
                "direction": "inbound",
                "author_name": "Ada",
                "body": "Hi",
            },
            {
                "direction": "outbound",
                "author_name": "Brand",
                "body": "Hello!",
            },
            {
                "direction": "inbound",
                "author_name": "Ada",
                "body": "Need help with my order",
            },
        ],
        platform="instagram",
        kind="dm",
        sentiment="neutral",
        brand_name="Draft Brand",
    )
    assert reply_prompt.PROMPT_VERSION == "reply-suggestion-v2"
    user = messages[1].content
    assert "Need help with my order" in user
    assert "[Customer — Ada]" in user
    assert "[Brand — Brand]" in user
    assert "Inbound message:" not in user


@pytest.mark.asyncio
async def test_ai_draft_returns_bilingual_and_persists(
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

    conv = await _seed_dm_conversation(
        db_session,
        org_id=org_id,
        brand_id=uuid.UUID(brand["id"]),
        account=account,
    )
    conversation_id = conv.id
    await inbox_repo.insert_inbound_message(
        db_session,
        organization_id=org_id,
        conversation_id=conversation_id,
        body="Hi there",
        lang="en",
        author_name="Ada",
        external_message_id=f"msg_{uuid.uuid4().hex[:8]}",
    )
    await db_session.commit()

    response = await client.post(
        f"/v1/orgs/{org_id}/inbox/conversations/{conversation_id}/ai-draft"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["suggestedReplyAr"]
    assert body["suggestedReplyEn"]

    await db_session.rollback()
    db_session.expire_all()
    refreshed = (
        await db_session.execute(
            select(inbox_models.Conversation).where(
                inbox_models.Conversation.id == conversation_id
            )
        )
    ).scalar_one()
    assert refreshed.suggested_reply_ar == body["suggestedReplyAr"]
    assert refreshed.suggested_reply_en == body["suggestedReplyEn"]
    assert refreshed.suggestion_decision_id is not None


@pytest.mark.asyncio
async def test_ai_draft_uses_newest_twenty_messages(
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

    conv = await _seed_dm_conversation(
        db_session,
        org_id=org_id,
        brand_id=uuid.UUID(brand["id"]),
        account=account,
    )
    base = utc_now() - timedelta(hours=REPLY_DRAFT_MESSAGE_LIMIT + 5)
    for i in range(REPLY_DRAFT_MESSAGE_LIMIT + 5):
        await inbox_repo.insert_synced_message(
            db_session,
            organization_id=org_id,
            conversation_id=conv.id,
            direction="inbound" if i % 2 == 0 else "outbound",
            body=f"msg-{i}",
            lang="en",
            author_name="Ada" if i % 2 == 0 else "Brand",
            external_message_id=f"ext_{i}_{uuid.uuid4().hex[:6]}",
            created_at=base + timedelta(minutes=i),
        )
    await db_session.commit()

    recent = await inbox_repo.list_recent_messages(
        db_session, conversation_id=conv.id, limit=REPLY_DRAFT_MESSAGE_LIMIT
    )
    assert len(recent) == REPLY_DRAFT_MESSAGE_LIMIT
    assert recent[0].body == "msg-5"
    assert recent[-1].body == f"msg-{REPLY_DRAFT_MESSAGE_LIMIT + 4}"

    response = await client.post(
        f"/v1/orgs/{org_id}/inbox/conversations/{conv.id}/ai-draft"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["suggestedReplyEn"]


@pytest.mark.asyncio
async def test_ai_draft_wrong_org_404(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    other_org_id, _other_user, other_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    channel = await _connect_instagram(client, org_id, brand["id"])

    await db_session.rollback()
    account = (
        await db_session.execute(
            select(SocialAccount).where(SocialAccount.id == uuid.UUID(channel["id"]))
        )
    ).scalar_one()
    conv = await _seed_dm_conversation(
        db_session,
        org_id=org_id,
        brand_id=uuid.UUID(brand["id"]),
        account=account,
    )
    await db_session.commit()

    _login(client, other_token)
    response = await client.post(
        f"/v1/orgs/{other_org_id}/inbox/conversations/{conv.id}/ai-draft"
    )
    assert response.status_code == 404
