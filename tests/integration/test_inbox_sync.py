"""Integration tests for inbox sync enqueue + backfill hydration."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.inbox import models as inbox_models
from app.features.inbox import service as inbox_service
from app.features.social_accounts.models import SocialAccount
from app.integrations.social import reset_fake_social_provider
from app.integrations.social.fakes import FakeSocialProvider
from app.integrations.social.ports import (
    InboxComment,
    InboxConversation,
    InboxConversationsResult,
    InboxMessage,
    InboxMessagesResult,
)
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZERNIO_API_KEY__t1", "fake-key-t1")
    monkeypatch.setenv("ZERNIO_API_KEY__t2", "fake-key-t2")
    monkeypatch.setenv("ZERNIO_API_KEY__t3", "fake-key-t3")
    reset_fake_social_provider()


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Inbox Sync Brand {uuid.uuid4().hex[:6]}",
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


class _HydratingFake(FakeSocialProvider):
    async def list_inbox_conversations(
        self,
        *,
        account_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> InboxConversationsResult:
        return InboxConversationsResult(
            conversations=[
                InboxConversation(
                    id="conv_hydrate_1",
                    account_id=account_id or "acc_1",
                    platform="instagram",
                    participant_name="Ada",
                    participant_handle="@ada",
                    last_message_at="2024-11-02T08:05:00Z",
                    last_message_preview="Thanks",
                    unread_count=1,
                )
            ],
            next_cursor=None,
            has_more=False,
        )

    async def list_inbox_messages(
        self,
        *,
        conversation_id: str,
        account_id: str,
        limit: int = 100,
        cursor: str | None = None,
        sort_order: str = "asc",
    ) -> InboxMessagesResult:
        return InboxMessagesResult(
            messages=[
                InboxMessage(
                    id="msg_in_1",
                    conversation_id=conversation_id,
                    account_id=account_id,
                    direction="incoming",
                    text="Hello from Ada",
                    sender_name="Ada",
                    created_at="2024-11-02T08:00:00Z",
                ),
                InboxMessage(
                    id="msg_out_1",
                    conversation_id=conversation_id,
                    account_id=account_id,
                    direction="outgoing",
                    text="Thanks for writing",
                    sender_name="Brand",
                    created_at="2024-11-02T08:05:00Z",
                ),
            ],
            next_cursor=None,
            has_more=False,
        )

    async def get_post_comments(
        self, *, post_id: str, account_id: str
    ) -> list[InboxComment]:
        return []


@pytest.mark.asyncio
async def test_manual_inbox_sync_enqueues_and_backfill_inserts_messages(
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

    # Connect enqueues inbox_sync with a time-bucketed unique_key.
    jobs = (
        await db_session.execute(
            text(
                "SELECT type, unique_key FROM jobs "
                "WHERE type = 'inbox_sync' AND organization_id = :org"
            ),
            {"org": str(org_id)},
        )
    ).all()
    assert any(row[0] == "inbox_sync" for row in jobs)
    assert any(
        row[1] is not None and str(row[1]).startswith(f"inbox_sync:{account.id}:")
        for row in jobs
    )

    # Finish the connect-time job so a manual Sync in the same 5m bucket can enqueue.
    await db_session.execute(
        text(
            "UPDATE jobs SET state = 'succeeded', finished_at = now() "
            "WHERE type = 'inbox_sync' AND organization_id = :org "
            "AND state IN ('queued', 'running')"
        ),
        {"org": str(org_id)},
    )
    await db_session.commit()

    sync = await client.post(
        f"/v1/orgs/{org_id}/inbox/sync",
        json={"skipClassify": True, "conversationLimit": 25, "mode": "initial"},
    )
    assert sync.status_code == 200, sync.text
    body = sync.json()
    assert body["enqueued"] >= 1
    assert channel["id"] in body["accountIds"]

    provider = _HydratingFake()
    count = await inbox_service.sync_backfill(
        db_session,
        social_account_id=account.id,
        provider=provider,
        skip_classify=True,
        conversation_limit=25,
        mode="initial",
    )
    await db_session.commit()
    assert count >= 1

    classify_jobs = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM jobs "
                "WHERE type = 'inbox_classify_message' AND organization_id = :org"
            ),
            {"org": str(org_id)},
        )
    ).scalar_one()
    assert int(classify_jobs) == 0

    convs = list(
        (
            await db_session.execute(
                select(inbox_models.Conversation).where(
                    inbox_models.Conversation.social_account_id == account.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(convs) == 1
    msgs = list(
        (
            await db_session.execute(
                select(inbox_models.ConversationMessage).where(
                    inbox_models.ConversationMessage.conversation_id == convs[0].id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(msgs) == 2
    assert {m.direction for m in msgs} == {"inbound", "outbound"}

    status = await client.get(f"/v1/orgs/{org_id}/inbox/sync-status")
    assert status.status_code == 200, status.text
    accounts = status.json()["accounts"]
    assert any(a["accountId"] == channel["id"] for a in accounts)
    matched = next(a for a in accounts if a["accountId"] == channel["id"])
    assert matched["lastSyncStatus"] == "ok"
    assert matched["lastSyncedAt"]
    assert matched.get("hasMore") is False


@pytest.mark.asyncio
async def test_inbox_poll_unique_key_allows_rerun(
    seed_member: SeedMember,
    db_session: AsyncSession,
    client: AsyncClient,
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    channel = await _connect_instagram(client, org_id, brand["id"])
    await db_session.rollback()

    account_id = uuid.UUID(channel["id"])
    key1 = inbox_service.inbox_sync_unique_key(account_id)
    key2 = inbox_service.inbox_sync_unique_key(account_id)
    assert key1 == key2
    assert key1.startswith(f"inbox_sync:{account_id}:")

    # Connect already enqueued in this bucket; finish it so poll can insert again.
    await db_session.execute(
        text(
            "UPDATE jobs SET state = 'succeeded', finished_at = now() "
            "WHERE type = 'inbox_sync' AND unique_key LIKE :prefix "
            "AND state IN ('queued', 'running')"
        ),
        {"prefix": f"inbox_sync:{account_id}:%"},
    )
    await db_session.commit()

    n1 = await inbox_service.enqueue_inbox_poll(db_session)
    await db_session.commit()
    # Second poll in the same 5m bucket should dedupe live duplicates.
    n2 = await inbox_service.enqueue_inbox_poll(db_session)
    await db_session.commit()
    assert n1 >= 1
    assert n2 == 0


class _PagingFake(FakeSocialProvider):
    """Two conversation pages so Sync more can resume from dm_cursor."""

    async def list_inbox_conversations(
        self,
        *,
        account_id: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> InboxConversationsResult:
        if cursor is None:
            return InboxConversationsResult(
                conversations=[
                    InboxConversation(
                        id="conv_page_1",
                        account_id=account_id or "acc_1",
                        platform="instagram",
                        participant_name="Ada",
                        participant_handle="@ada",
                        last_message_at="2024-11-02T08:05:00Z",
                        last_message_preview="First page",
                        unread_count=1,
                    )
                ],
                next_cursor="cursor_page_2",
                has_more=True,
            )
        return InboxConversationsResult(
            conversations=[
                InboxConversation(
                    id="conv_page_2",
                    account_id=account_id or "acc_1",
                    platform="instagram",
                    participant_name="Bob",
                    participant_handle="@bob",
                    last_message_at="2024-11-01T08:05:00Z",
                    last_message_preview="Second page",
                    unread_count=0,
                )
            ],
            next_cursor=None,
            has_more=False,
        )

    async def list_inbox_messages(
        self,
        *,
        conversation_id: str,
        account_id: str,
        limit: int = 100,
        cursor: str | None = None,
        sort_order: str = "asc",
    ) -> InboxMessagesResult:
        return InboxMessagesResult(
            messages=[
                InboxMessage(
                    id=f"msg_{conversation_id}",
                    conversation_id=conversation_id,
                    account_id=account_id,
                    direction="incoming",
                    text=f"Hello from {conversation_id}",
                    sender_name="User",
                    created_at="2024-11-02T08:00:00Z",
                ),
            ],
            next_cursor=None,
            has_more=False,
        )

    async def get_post_comments(
        self, *, post_id: str, account_id: str
    ) -> list[InboxComment]:
        return []


@pytest.mark.asyncio
async def test_inbox_sync_skips_ai_and_pages_with_cursor(
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

    provider = _PagingFake()
    count1 = await inbox_service.sync_backfill(
        db_session,
        social_account_id=account.id,
        provider=provider,
        skip_classify=True,
        conversation_limit=25,
        mode="initial",
    )
    await db_session.commit()
    assert count1 == 1

    classify_jobs = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM jobs "
                "WHERE type = 'inbox_classify_message' AND organization_id = :org"
            ),
            {"org": str(org_id)},
        )
    ).scalar_one()
    assert int(classify_jobs) == 0

    status1 = await client.get(f"/v1/orgs/{org_id}/inbox/sync-status")
    assert status1.status_code == 200, status1.text
    matched1 = next(
        a for a in status1.json()["accounts"] if a["accountId"] == channel["id"]
    )
    assert matched1["hasMore"] is True

    count2 = await inbox_service.sync_backfill(
        db_session,
        social_account_id=account.id,
        provider=provider,
        skip_classify=True,
        conversation_limit=25,
        mode="more",
    )
    await db_session.commit()
    assert count2 == 1

    convs = list(
        (
            await db_session.execute(
                select(inbox_models.Conversation).where(
                    inbox_models.Conversation.social_account_id == account.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(convs) == 2
    assert {c.external_thread_id for c in convs} == {"conv_page_1", "conv_page_2"}

    classify_jobs_after = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM jobs "
                "WHERE type = 'inbox_classify_message' AND organization_id = :org"
            ),
            {"org": str(org_id)},
        )
    ).scalar_one()
    assert int(classify_jobs_after) == 0

    status2 = await client.get(f"/v1/orgs/{org_id}/inbox/sync-status")
    assert status2.status_code == 200, status2.text
    matched2 = next(
        a for a in status2.json()["accounts"] if a["accountId"] == channel["id"]
    )
    assert matched2["hasMore"] is False
