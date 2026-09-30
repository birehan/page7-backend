"""Phase 12 Stage 6 — exactly-once inbox reply send + reconcile."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import utc_now
from app.features.inbox import models as inbox_models
from app.features.inbox import repository as inbox_repo
from app.features.inbox import service as inbox_service
from app.features.social_accounts.models import SocialAccount
from app.integrations.social import reset_fake_social_provider
from app.jobs.handlers.inbox import (
    handle_inbox_send_reply,
    handle_reconcile_inbox_replies,
)
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Reply Brand {uuid.uuid4().hex[:6]}",
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


@pytest.mark.asyncio
async def test_happy_path_send_marks_sent(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    channel = await _connect_instagram(client, org_id, brand["id"])

    await db_session.rollback()
    account = (
        await db_session.execute(
            select(SocialAccount).where(SocialAccount.id == uuid.UUID(channel["id"]))
        )
    ).scalar_one()

    fake = reset_fake_social_provider()
    fake.inbox_reply_script = "sent"

    conv = await _seed_dm_conversation(
        db_session,
        org_id=org_id,
        brand_id=uuid.UUID(brand["id"]),
        account=account,
    )
    msg = await inbox_service.send_reply(
        db_session,
        conversation_id=conv.id,
        body="Thanks for reaching out!",
        author_user_id=user_id,
        author_name="Owner",
    )
    message_id = msg.id
    await db_session.commit()

    await handle_inbox_send_reply({"message_id": str(message_id)})
    db_session.expire_all()

    live = (
        await db_session.execute(
            select(inbox_models.ConversationMessage).where(
                inbox_models.ConversationMessage.id == message_id
            )
        )
    ).scalar_one()
    assert live.delivery_status == "sent"
    assert live.external_message_id
    assert live.sent_at is not None
    assert fake.send_call_count == 1


@pytest.mark.asyncio
async def test_idempotent_replayed_keeps_single_send(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    channel = await _connect_instagram(client, org_id, brand["id"])

    await db_session.rollback()
    account = (
        await db_session.execute(
            select(SocialAccount).where(SocialAccount.id == uuid.UUID(channel["id"]))
        )
    ).scalar_one()

    fake = reset_fake_social_provider()
    fake.inbox_reply_script = "sent"

    conv = await _seed_dm_conversation(
        db_session,
        org_id=org_id,
        brand_id=uuid.UUID(brand["id"]),
        account=account,
    )
    msg = await inbox_service.send_reply(
        db_session,
        conversation_id=conv.id,
        body="Thanks again!",
        author_user_id=user_id,
        author_name="Owner",
    )
    message_id = msg.id
    await db_session.commit()

    await handle_inbox_send_reply({"message_id": str(message_id)})
    await handle_inbox_send_reply({"message_id": str(message_id)})
    db_session.expire_all()

    live = (
        await db_session.execute(
            select(inbox_models.ConversationMessage).where(
                inbox_models.ConversationMessage.id == message_id
            )
        )
    ).scalar_one()
    assert live.delivery_status == "sent"
    # Second handler sees delivery_status=sent and returns without calling provider.
    assert fake.send_call_count == 1


@pytest.mark.asyncio
async def test_ambiguous_no_second_send_reconcile_marks_sent(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    channel = await _connect_instagram(client, org_id, brand["id"])

    await db_session.rollback()
    account = (
        await db_session.execute(
            select(SocialAccount).where(SocialAccount.id == uuid.UUID(channel["id"]))
        )
    ).scalar_one()

    fake = reset_fake_social_provider()
    fake.inbox_reply_script = "ambiguous_5xx_after_accept"

    conv = await _seed_dm_conversation(
        db_session,
        org_id=org_id,
        brand_id=uuid.UUID(brand["id"]),
        account=account,
    )
    body = "Ambiguous reply body unique"
    msg = await inbox_service.send_reply(
        db_session,
        conversation_id=conv.id,
        body=body,
        author_user_id=user_id,
        author_name="Owner",
    )
    message_id = msg.id
    await db_session.commit()

    await handle_inbox_send_reply({"message_id": str(message_id)})
    db_session.expire_all()

    live = (
        await db_session.execute(
            select(inbox_models.ConversationMessage).where(
                inbox_models.ConversationMessage.id == message_id
            )
        )
    ).scalar_one()
    assert live.delivery_status == "pending"
    assert live.error_code == "OUTCOME_UNKNOWN"
    assert fake.send_call_count == 1

    # Job retry must NOT second-send.
    await handle_inbox_send_reply({"message_id": str(message_id)})
    db_session.expire_all()
    assert fake.send_call_count == 1

    # Force into reconcile window (pending older than grace).
    await db_session.execute(
        update(inbox_models.ConversationMessage)
        .where(inbox_models.ConversationMessage.id == message_id)
        .values(created_at=utc_now() - timedelta(minutes=5))
    )
    await db_session.commit()

    await handle_reconcile_inbox_replies({})
    db_session.expire_all()

    live2 = (
        await db_session.execute(
            select(inbox_models.ConversationMessage).where(
                inbox_models.ConversationMessage.id == message_id
            )
        )
    ).scalar_one()
    assert live2.delivery_status == "sent"
    assert live2.external_message_id
    assert fake.send_call_count == 1
