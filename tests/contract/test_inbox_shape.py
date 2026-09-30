"""Contract tests for Phase 12 inbox endpoints against api-contract.json."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import jsonschema
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import utc_now
from app.features.inbox import models as inbox_models
from app.features.inbox import repository as inbox_repo
from app.features.social_accounts.models import SocialAccount
from app.integrations.social import reset_fake_social_provider
from tests.contract.conftest import find_endpoint
from tests.db_fixtures import SeedMember


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
    monkeypatch.setenv("ZERNIO_API_KEY__t1", "fake-key-t1")
    monkeypatch.setenv("ZERNIO_API_KEY__t2", "fake-key-t2")
    monkeypatch.setenv("ZERNIO_API_KEY__t3", "fake-key-t3")
    reset_fake_social_provider()


async def _seed_conversation(
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


@pytest.mark.asyncio
async def test_inbox_endpoints_match_the_frontend_contract(
    client: AsyncClient,
    seed_member: SeedMember,
    db_session: AsyncSession,
    api_contract: dict[str, Any],
) -> None:
    org_id, user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    channel = await _connect_instagram(client, org_id, brand["id"])

    await db_session.rollback()
    account = await db_session.get(SocialAccount, uuid.UUID(channel["id"]))
    assert account is not None
    conversation = await _seed_conversation(
        db_session,
        org_id=org_id,
        brand_id=uuid.UUID(brand["id"]),
        account=account,
    )
    await inbox_repo.insert_inbound_message(
        db_session,
        organization_id=org_id,
        conversation_id=conversation.id,
        body="Hello from Ada",
        lang="en",
        author_name="Ada",
        external_message_id=f"msg_{uuid.uuid4().hex[:8]}",
    )
    await db_session.commit()

    listed = await client.get(f"/v1/orgs/{org_id}/inbox/conversations")
    assert listed.status_code == 200, listed.text
    jsonschema.validate(
        instance=listed.json(),
        schema=find_endpoint(
            api_contract, "inbox", "GET", path_contains="/inbox/conversations"
        )["response"],
    )
    assert len(listed.json()["items"]) >= 1
    conversation_id = listed.json()["items"][0]["id"]

    messages = await client.get(
        f"/v1/orgs/{org_id}/inbox/conversations/{conversation_id}/messages"
    )
    assert messages.status_code == 200, messages.text
    jsonschema.validate(
        instance=messages.json(),
        schema=find_endpoint(
            api_contract, "inbox", "GET", path_contains="/messages"
        )["response"],
    )

    replied = await client.post(
        f"/v1/orgs/{org_id}/inbox/conversations/{conversation_id}/reply",
        json={"body": "Thanks Ada!"},
        headers={"Idempotency-Key": f"inbox-reply:{conversation_id}:{uuid.uuid4()}"},
    )
    assert replied.status_code == 200, replied.text
    jsonschema.validate(
        instance=replied.json(),
        schema=find_endpoint(
            api_contract, "inbox", "POST", path_contains="/reply"
        )["response"],
    )

    assigned = await client.post(
        f"/v1/orgs/{org_id}/inbox/conversations/{conversation_id}/assign",
        json={"assigneeId": str(user_id)},
    )
    assert assigned.status_code == 200, assigned.text
    jsonschema.validate(
        instance=assigned.json(),
        schema=find_endpoint(
            api_contract, "inbox", "POST", path_contains="/assign"
        )["response"],
    )

    tagged = await client.put(
        f"/v1/orgs/{org_id}/inbox/conversations/{conversation_id}/tags",
        json={"tags": ["vip"]},
    )
    assert tagged.status_code == 200, tagged.text
    jsonschema.validate(
        instance=tagged.json(),
        schema=find_endpoint(
            api_contract, "inbox", "PUT", path_contains="/tags"
        )["response"],
    )

    resolved = await client.post(
        f"/v1/orgs/{org_id}/inbox/conversations/{conversation_id}/resolve"
    )
    assert resolved.status_code == 200, resolved.text
    reopened = await client.post(
        f"/v1/orgs/{org_id}/inbox/conversations/{conversation_id}/reopen"
    )
    assert reopened.status_code == 200, reopened.text
    escalated = await client.post(
        f"/v1/orgs/{org_id}/inbox/conversations/{conversation_id}/escalate"
    )
    assert escalated.status_code == 200, escalated.text

    saved_list = await client.get(f"/v1/orgs/{org_id}/inbox/saved-replies")
    assert saved_list.status_code == 200, saved_list.text
    jsonschema.validate(
        instance=saved_list.json(),
        schema=find_endpoint(
            api_contract, "inbox", "GET", path_contains="/saved-replies"
        )["response"],
    )

    created = await client.post(
        f"/v1/orgs/{org_id}/inbox/saved-replies",
        json={
            "title": "Hours",
            "bodyAr": "نعمل من ٩ إلى ٥",
            "bodyEn": "We are open 9–5",
        },
    )
    assert created.status_code == 201, created.text
    jsonschema.validate(
        instance=created.json(),
        schema=find_endpoint(
            api_contract, "inbox", "POST", path_contains="/saved-replies"
        )["response"],
    )
    deleted = await client.delete(
        f"/v1/orgs/{org_id}/inbox/saved-replies/{created.json()['id']}"
    )
    assert deleted.status_code == 204, deleted.text


@pytest.mark.asyncio
async def test_messages_endpoint_returns_ascending_order(
    client: AsyncClient,
    seed_member: SeedMember,
    db_session: AsyncSession,
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    channel = await _connect_instagram(client, org_id, brand["id"])

    await db_session.rollback()
    account = await db_session.get(SocialAccount, uuid.UUID(channel["id"]))
    assert account is not None
    conversation = await _seed_conversation(
        db_session,
        org_id=org_id,
        brand_id=uuid.UUID(brand["id"]),
        account=account,
    )

    base = utc_now() - timedelta(minutes=10)
    bodies = ["first", "second", "third"]
    for i, body in enumerate(bodies):
        msg = await inbox_repo.insert_inbound_message(
            db_session,
            organization_id=org_id,
            conversation_id=conversation.id,
            body=body,
            lang="en",
            author_name="Ada",
            external_message_id=f"ord_{uuid.uuid4().hex[:8]}",
        )
        assert msg is not None
        msg.created_at = base + timedelta(minutes=i)
    await db_session.commit()

    response = await client.get(
        f"/v1/orgs/{org_id}/inbox/conversations/{conversation.id}/messages"
    )
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [m["body"] for m in items] == bodies
    created_ats = [m["createdAt"] for m in items]
    assert created_ats == sorted(created_ats)
