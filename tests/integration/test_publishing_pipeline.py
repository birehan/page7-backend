"""Phase 10 publishing pipeline — claim, crash resume, freeze, lateness, velocity."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import utc_now
from app.features.publishing.models import Publication
from app.features.social_accounts.models import SocialAccount
from app.integrations.social import reset_fake_social_provider
from app.jobs.handlers.publish_post import handle as handle_publish_post
from app.jobs.handlers.reconcile_publications import (
    ACCEPTED_POLL_AFTER,
    OUTCOME_UNKNOWN_AFTER,
    SENT_RESUME_AFTER,
)
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Pub Brand {uuid.uuid4().hex[:6]}",
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


def _create_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "platform": "instagram",
        "scheduledAt": (datetime.now(UTC) + timedelta(hours=1))
        .isoformat()
        .replace("+00:00", "Z"),
        "variants": [
            {
                "lang": "ar",
                "dialect": "gulf",
                "caption": "نشر تجريبي",
                "hashtags": ["#قهوة"],
            }
        ],
    }
    body.update(overrides)
    return body


async def _to_scheduled(
    client: AsyncClient, base: str, *, scheduled_at: str | None = None
) -> dict[str, Any]:
    body = _create_body(**({"scheduledAt": scheduled_at} if scheduled_at else {}))
    created = await client.post(base, json=body)
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]
    submitted = await client.post(f"{base}/{post_id}/submit")
    assert submitted.status_code == 200, submitted.text
    approved = await client.post(f"{base}/{post_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "scheduled"
    return approved.json()  # type: ignore[no-any-return]


async def _epoch(db_session: AsyncSession, post_id: str) -> int:
    row = (
        await db_session.execute(
            text("SELECT schedule_epoch FROM posts WHERE id = :id"),
            {"id": post_id},
        )
    ).fetchone()
    assert row is not None
    return int(row.schedule_epoch)


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    reset_fake_social_provider()


@pytest.mark.asyncio
async def test_happy_path_schedule_handle_publishes(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    await _connect_instagram(client, org_id, brand["id"])
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    post = await _to_scheduled(
        client,
        base,
        scheduled_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )
    await db_session.rollback()
    epoch = await _epoch(db_session, post["id"])

    fake = reset_fake_social_provider()
    await handle_publish_post(
        {
            "post_id": post["id"],
            "epoch": epoch,
            "organization_id": str(org_id),
            "trigger": "scheduled",
            "run_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    )

    await db_session.rollback()
    row = (
        await db_session.execute(
            text(
                "SELECT status, zernio_post_id FROM posts WHERE id = :id"
            ),
            {"id": post["id"]},
        )
    ).fetchone()
    assert row is not None
    assert row.status == "published"
    assert row.zernio_post_id
    assert fake.publish_call_count() == 1

    pubs = (
        await db_session.execute(
            select(Publication).where(Publication.post_id == uuid.UUID(post["id"]))
        )
    ).scalars().all()
    assert len(pubs) == 1
    assert pubs[0].status == "published"


@pytest.mark.asyncio
async def test_crash_resume_existing_post_one_published_row(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    await _connect_instagram(client, org_id, brand["id"])
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    post = await _to_scheduled(
        client,
        base,
        scheduled_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )

    await db_session.rollback()
    epoch = await _epoch(db_session, post["id"])

    fake = reset_fake_social_provider()
    # First claim creates sent + publishing; force non_definitive so job would retry.
    fake.publish_script = "non_definitive"
    from app.jobs.errors import RetryableError

    with pytest.raises(RetryableError):
        await handle_publish_post(
            {
                "post_id": post["id"],
                "epoch": epoch,
                "organization_id": str(org_id),
                "trigger": "scheduled",
                "run_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        )

    await db_session.rollback()
    pub = (
        await db_session.execute(
            select(Publication).where(Publication.post_id == uuid.UUID(post["id"]))
        )
    ).scalar_one()
    assert pub.status == "sent"
    assert fake.publish_call_count() == 1

    # Resume: fake returns existing for same idempotency key after we flip script
    # and seed via a real created call path — switch to created then existing replay.
    fake.publish_script = "created"
    # Clear idempotency so a created lands, then second resume uses existing map.
    # Actually prior call was non_definitive and did NOT store — so created works.
    await handle_publish_post(
        {
            "post_id": post["id"],
            "epoch": epoch,
            "organization_id": str(org_id),
            "trigger": "resume",
            "run_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    )

    await db_session.rollback()
    pubs = (
        await db_session.execute(
            select(Publication).where(Publication.post_id == uuid.UUID(post["id"]))
        )
    ).scalars().all()
    assert len(pubs) == 1
    assert pubs[0].status == "published"
    # One non_definitive + one created (or existing on further resume)
    assert fake.publish_call_count() >= 2

    # Third resume with same key should be existing / no new publication row
    await handle_publish_post(
        {
            "post_id": post["id"],
            "epoch": epoch,
            "organization_id": str(org_id),
            "trigger": "resume",
            "run_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    )
    await db_session.rollback()
    pubs2 = (
        await db_session.execute(
            select(Publication).where(Publication.post_id == uuid.UUID(post["id"]))
        )
    ).scalars().all()
    assert len(pubs2) == 1


@pytest.mark.asyncio
async def test_freeze_between_tx1_and_http(
    client: AsyncClient,
    seed_member: SeedMember,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    await _connect_instagram(client, org_id, brand["id"])
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    post = await _to_scheduled(
        client,
        base,
        scheduled_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )

    await db_session.rollback()
    epoch = await _epoch(db_session, post["id"])

    fake = reset_fake_social_provider()
    calls = {"n": 0}

    async def _freeze(session: AsyncSession, organization_id: uuid.UUID) -> bool:
        del session, organization_id
        calls["n"] += 1
        return calls["n"] >= 2

    monkeypatch.setattr(
        "app.jobs.handlers.publish_post.check_freeze", _freeze
    )

    await handle_publish_post(
        {
            "post_id": post["id"],
            "epoch": epoch,
            "organization_id": str(org_id),
            "trigger": "scheduled",
            "run_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    )

    await db_session.rollback()
    assert fake.publish_call_count() == 0
    pub = (
        await db_session.execute(
            select(Publication).where(Publication.post_id == uuid.UUID(post["id"]))
        )
    ).scalar_one()
    assert pub.status == "skipped"
    row = (
        await db_session.execute(
            text("SELECT status, last_error FROM posts WHERE id = :id"),
            {"id": post["id"]},
        )
    ).fetchone()
    assert row is not None
    assert row.status == "failed"
    assert row.last_error["code"] == "FROZEN"


@pytest.mark.asyncio
async def test_stale_epoch_noop(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    await _connect_instagram(client, org_id, brand["id"])
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    post = await _to_scheduled(client, base)
    await db_session.rollback()
    epoch = await _epoch(db_session, post["id"])

    fake = reset_fake_social_provider()
    await handle_publish_post(
        {
            "post_id": post["id"],
            "epoch": epoch + 99,
            "organization_id": str(org_id),
            "trigger": "scheduled",
            "run_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    )
    assert fake.publish_call_count() == 0
    await db_session.rollback()
    row = (
        await db_session.execute(
            text("SELECT status FROM posts WHERE id = :id"),
            {"id": post["id"]},
        )
    ).fetchone()
    assert row is not None
    assert row.status == "scheduled"


@pytest.mark.asyncio
async def test_missed_schedule_zero_publish_calls(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    await _connect_instagram(client, org_id, brand["id"])
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    post = await _to_scheduled(
        client,
        base,
        scheduled_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )

    await db_session.rollback()
    epoch = await _epoch(db_session, post["id"])

    fake = reset_fake_social_provider()
    past = (datetime.now(UTC) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    await handle_publish_post(
        {
            "post_id": post["id"],
            "epoch": epoch,
            "organization_id": str(org_id),
            "trigger": "scheduled",
            "run_at": past,
        }
    )
    assert fake.publish_call_count() == 0
    await db_session.rollback()
    row = (
        await db_session.execute(
            text("SELECT status, last_error FROM posts WHERE id = :id"),
            {"id": post["id"]},
        )
    ).fetchone()
    assert row is not None
    assert row.status == "failed"
    assert row.last_error["code"] == "MISSED_SCHEDULE"


@pytest.mark.asyncio
async def test_velocity_hold_no_publication(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    await _connect_instagram(client, org_id, brand["id"])
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    post = await _to_scheduled(
        client,
        base,
        scheduled_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    )

    await db_session.rollback()
    epoch = await _epoch(db_session, post["id"])

    await db_session.rollback()
    account = (
        await db_session.execute(
            select(SocialAccount).where(
                SocialAccount.brand_id == uuid.UUID(brand["id"]),
                SocialAccount.platform == "instagram",
            )
        )
    ).scalar_one()
    assert account.credential_id is not None
    assert account.zernio_profile_id is not None

    now = utc_now()
    from app.features.auth.models import User
    from app.features.posts.models import Post as PostModel

    user = (
        await db_session.execute(select(User).limit(1))
    ).scalar_one()
    for i in range(25):
        filler = PostModel(
            organization_id=org_id,
            brand_id=uuid.UUID(brand["id"]),
            platform="instagram",
            status="published",
            scheduled_at=now,
            published_at=now,
            created_by=user.id,
            variants=[{"lang": "en", "caption": f"filler {i}"}],
        )
        db_session.add(filler)
        await db_session.flush()
        db_session.add(
            Publication(
                organization_id=org_id,
                brand_id=uuid.UUID(brand["id"]),
                post_id=filler.id,
                social_account_id=account.id,
                zernio_profile_id=account.zernio_profile_id,
                credential_id=account.credential_id,
                attempt_no=1,
                trigger="scheduled",
                idempotency_key=f"vel:{filler.id}:1",
                status="published",
                scheduled_for=now,
                sent_at=now,
                completed_at=now - timedelta(minutes=i % 50),
            )
        )
    await db_session.commit()

    fake = reset_fake_social_provider()
    from app.jobs.errors import RetryableError

    with pytest.raises(RetryableError) as exc_info:
        await handle_publish_post(
            {
                "post_id": post["id"],
                "epoch": epoch,
                "organization_id": str(org_id),
                "trigger": "scheduled",
                "run_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            }
        )
    assert exc_info.value.after is not None
    assert fake.publish_call_count() == 0

    await db_session.rollback()
    claim_pubs = (
        await db_session.execute(
            select(Publication).where(Publication.post_id == uuid.UUID(post["id"]))
        )
    ).scalars().all()
    assert claim_pubs == []
    row = (
        await db_session.execute(
            text("SELECT status FROM posts WHERE id = :id"),
            {"id": post["id"]},
        )
    ).fetchone()
    assert row is not None
    assert row.status == "scheduled"


def test_reconcile_threshold_constants() -> None:
    assert SENT_RESUME_AFTER == timedelta(minutes=3)
    assert ACCEPTED_POLL_AFTER == timedelta(minutes=5)
    assert OUTCOME_UNKNOWN_AFTER == timedelta(minutes=30)
