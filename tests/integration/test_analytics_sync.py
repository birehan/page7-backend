"""Phase 12 analytics_sync — FakeSocialProvider fleet delta protocol."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.analytics.models import AnalyticsSyncState, MetricSnapshot
from app.features.publishing.models import Publication
from app.features.social_accounts.models import SocialAccount
from app.integrations.errors import AnalyticsCursorExpired
from app.integrations.social import reset_fake_social_provider
from app.integrations.social.ports import AnalyticsDeltaResult
from app.jobs.handlers.analytics import handle_analytics_sync
from app.jobs.handlers.publish_post import handle as handle_publish_post
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Analytics Brand {uuid.uuid4().hex[:6]}",
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


async def _credential_id_for_brand(
    db_session: AsyncSession, brand_id: uuid.UUID
) -> uuid.UUID:
    account = (
        await db_session.execute(
            select(SocialAccount).where(
                SocialAccount.brand_id == brand_id,
                SocialAccount.platform == "instagram",
            )
        )
    ).scalar_one()
    assert account.credential_id is not None
    return account.credential_id


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZERNIO_API_KEY__t1", "fake-key-t1")
    monkeypatch.setenv("ZERNIO_API_KEY__t2", "fake-key-t2")
    monkeypatch.setenv("ZERNIO_API_KEY__t3", "fake-key-t3")
    reset_fake_social_provider()


@pytest.mark.asyncio
async def test_empty_page_does_not_advance_cursor(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = uuid.UUID(brand["id"])
    await _connect_instagram(client, org_id, brand["id"])

    await db_session.rollback()
    credential_id = await _credential_id_for_brand(db_session, brand_id)

    fake = reset_fake_social_provider()
    fake.analytics_delta_script = "empty_page"

    await handle_analytics_sync({})
    await db_session.rollback()

    state = (
        await db_session.execute(
            select(AnalyticsSyncState).where(
                AnalyticsSyncState.credential_id == credential_id
            )
        )
    ).scalar_one()
    assert state.bootstrapped_at is not None
    assert state.last_sync_status == "ok"
    first_cursor = state.last_cursor
    assert first_cursor

    await handle_analytics_sync({})
    await db_session.rollback()

    state2 = (
        await db_session.execute(
            select(AnalyticsSyncState).where(
                AnalyticsSyncState.credential_id == credential_id
            )
        )
    ).scalar_one()
    assert state2.last_cursor == first_cursor
    assert state2.last_sync_status == "ok"


@pytest.mark.asyncio
async def test_402_gates_without_retry(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = uuid.UUID(brand["id"])
    await _connect_instagram(client, org_id, brand["id"])

    await db_session.rollback()
    credential_id = await _credential_id_for_brand(db_session, brand_id)

    # Ensure this credential has no prior sync state from other tests.
    await db_session.execute(
        text("DELETE FROM analytics_sync_state WHERE credential_id = :id"),
        {"id": str(credential_id)},
    )
    await db_session.commit()

    fake = reset_fake_social_provider()
    fake.analytics_delta_script = "addon_required"

    await handle_analytics_sync({})
    await db_session.rollback()

    state = (
        await db_session.execute(
            select(AnalyticsSyncState).where(
                AnalyticsSyncState.credential_id == credential_id
            )
        )
    ).scalar_one()
    assert state.last_sync_status == "gated"
    assert state.last_error == "analytics_addon_required"
    assert state.bootstrapped_at is None


@pytest.mark.asyncio
async def test_non_empty_upserts_metric_snapshot(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = uuid.UUID(brand["id"])
    await _connect_instagram(client, org_id, brand["id"])
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    body = {
        "platform": "instagram",
        "scheduledAt": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "variants": [
            {
                "lang": "ar",
                "dialect": "gulf",
                "caption": "تحليلات",
                "hashtags": ["#تجربة"],
            }
        ],
    }
    created = await client.post(base, json=body)
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]
    assert (await client.post(f"{base}/{post_id}/submit")).status_code == 200
    assert (await client.post(f"{base}/{post_id}/approve")).status_code == 200

    await db_session.rollback()
    credential_id = await _credential_id_for_brand(db_session, brand_id)
    epoch_row = (
        await db_session.execute(
            text("SELECT schedule_epoch FROM posts WHERE id = :id"),
            {"id": post_id},
        )
    ).fetchone()
    assert epoch_row is not None

    fake = reset_fake_social_provider()
    await handle_publish_post(
        {
            "post_id": post_id,
            "epoch": int(epoch_row.schedule_epoch),
            "organization_id": str(org_id),
            "trigger": "scheduled",
            "run_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    )
    await db_session.rollback()

    pub = (
        await db_session.execute(
            select(Publication).where(Publication.post_id == uuid.UUID(post_id))
        )
    ).scalar_one()
    assert pub.zernio_post_id
    zernio_post_id = pub.zernio_post_id

    from app.integrations.social.fakes import _sample_delta_entry

    async def delta_for_post(
        *, cursor: str | None = None, limit: int = 50
    ) -> AnalyticsDeltaResult:
        _ = cursor, limit
        return AnalyticsDeltaResult(
            data=[_sample_delta_entry(zernio_post_id)],
            next_cursor="cursor_after_1",
            has_more=False,
        )

    fake.get_analytics_delta = delta_for_post  # type: ignore[method-assign]
    await handle_analytics_sync({})
    await db_session.rollback()

    snaps = (
        await db_session.execute(
            select(MetricSnapshot).where(MetricSnapshot.post_id == uuid.UUID(post_id))
        )
    ).scalars().all()
    assert len(snaps) >= 1
    assert snaps[0].likes == 342
    assert snaps[0].impressions == 15420

    state = (
        await db_session.execute(
            select(AnalyticsSyncState).where(
                AnalyticsSyncState.credential_id == credential_id
            )
        )
    ).scalar_one()
    assert state.last_cursor == "cursor_after_1"
    assert state.last_sync_status == "ok"


@pytest.mark.asyncio
async def test_cursor_expired_re_bootstraps(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = uuid.UUID(brand["id"])
    await _connect_instagram(client, org_id, brand["id"])

    await db_session.rollback()
    credential_id = await _credential_id_for_brand(db_session, brand_id)
    await db_session.execute(
        text("DELETE FROM analytics_sync_state WHERE credential_id = :id"),
        {"id": str(credential_id)},
    )
    await db_session.commit()

    fake = reset_fake_social_provider()
    fake.analytics_delta_script = "empty_page"
    await handle_analytics_sync({})
    await db_session.rollback()

    state = (
        await db_session.execute(
            select(AnalyticsSyncState).where(
                AnalyticsSyncState.credential_id == credential_id
            )
        )
    ).scalar_one()
    assert state.bootstrapped_at is not None
    old_bootstrapped = state.bootstrapped_at

    calls = {"n": 0}
    original_delta = fake.get_analytics_delta

    async def flaky_delta(
        *, cursor: str | None = None, limit: int = 50
    ) -> AnalyticsDeltaResult:
        calls["n"] += 1
        if cursor is not None and calls["n"] == 1:
            raise AnalyticsCursorExpired("stale")
        return await original_delta(cursor=cursor, limit=limit)

    fake.get_analytics_delta = flaky_delta  # type: ignore[method-assign]
    fake.analytics_delta_script = "empty_page"

    await handle_analytics_sync({})
    await db_session.rollback()

    state2 = (
        await db_session.execute(
            select(AnalyticsSyncState).where(
                AnalyticsSyncState.credential_id == credential_id
            )
        )
    ).scalar_one()
    assert state2.bootstrapped_at is not None
    assert state2.bootstrapped_at >= old_bootstrapped
    assert state2.last_sync_status == "ok"
    assert calls["n"] >= 2
