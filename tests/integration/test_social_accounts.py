"""Phase 9 social accounts integration — real Postgres, FakeSocialProvider."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.social_accounts.models import SocialAccount, WebhookEvent
from app.integrations.social import reset_fake_social_provider
from app.jobs.queue import claim
from tests.db_fixtures import SeedMember

_WEBHOOK_SECRET = "test-webhook-secret"  # noqa: S105


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Social Brand {uuid.uuid4().hex[:6]}",
            "industry": "retail",
            "city": "Riyadh",
            "website": "https://example.sa/",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
def _webhook_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZERNIO_WEBHOOK_SECRET__t1", _WEBHOOK_SECRET)
    monkeypatch.setenv("ZERNIO_API_KEY__t1", "fake-key-t1")
    monkeypatch.setenv("ZERNIO_API_KEY__t2", "fake-key-t2")
    monkeypatch.setenv("ZERNIO_API_KEY__t3", "fake-key-t3")
    reset_fake_social_provider()


@pytest.mark.asyncio
async def test_create_brand_creates_five_placeholders(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)

    await db_session.rollback()
    rows = (
        (
            await db_session.execute(
                select(SocialAccount).where(SocialAccount.brand_id == uuid.UUID(brand["id"]))
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 5
    assert {r.platform for r in rows} == {
        "instagram",
        "facebook",
        "tiktok",
        "snapchat",
        "whatsapp",
    }
    assert all(r.status == "disconnected" for r in rows)


@pytest.mark.asyncio
async def test_list_channels_returns_five(client: AsyncClient, seed_member: SeedMember) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)

    response = await client.get(f"/v1/orgs/{org_id}/brands/{brand['id']}/channels")
    assert response.status_code == 200, response.text
    channels = response.json()
    assert len(channels) == 5
    assert all(c["connectedAt"] == "" for c in channels)


@pytest.mark.asyncio
async def test_oauth_url_returns_authorize_url(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    from app.integrations.social import reset_fake_social_provider

    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    fake = reset_fake_social_provider()

    response = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/instagram/oauth-url"
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert "authorizeUrl" in body
    assert "connected=instagram" in body["authorizeUrl"]
    assert "/v1/integrations/zernio/callback" in body["authorizeUrl"]
    assert "zernio.test" not in body["authorizeUrl"]
    assert body["state"]

    connect_calls = [c for c in fake.calls if c[0] == "get_connect_url"]
    assert len(connect_calls) == 1
    redirect = connect_calls[0][1]["redirect_url"]
    assert "state=" in redirect
    assert body["state"] in redirect
    # Default return path encodes as `c.` (settings/channels).
    assert body["state"].startswith("c.")


@pytest.mark.asyncio
async def test_oauth_url_return_path_onboarding(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)

    response = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/facebook/oauth-url",
        json={"returnPath": "/onboarding"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"].startswith("o.")


@pytest.mark.asyncio
async def test_connect_is_idempotent(client: AsyncClient, seed_member: SeedMember) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)

    oauth = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/instagram/oauth-url"
    )
    assert oauth.status_code == 200, oauth.text

    channels = (await client.get(f"/v1/orgs/{org_id}/brands/{brand['id']}/channels")).json()
    ig = next(c for c in channels if c["platform"] == "instagram")

    first = await client.post(f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/{ig['id']}/connect")
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "connected"
    handle = first.json()["handle"]

    second = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/{ig['id']}/connect"
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "connected"
    assert second.json()["handle"] == handle
    assert second.json()["id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_delete_brand_disconnects_connected_account(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)

    await client.post(f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/instagram/oauth-url")
    channels = (await client.get(f"/v1/orgs/{org_id}/brands/{brand['id']}/channels")).json()
    ig = next(c for c in channels if c["platform"] == "instagram")
    connected = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/{ig['id']}/connect"
    )
    assert connected.json()["status"] == "connected"

    delete = await client.delete(f"/v1/orgs/{org_id}/brands/{brand['id']}")
    assert delete.status_code == 204, delete.text

    await db_session.rollback()
    rows = (
        (
            await db_session.execute(
                select(SocialAccount).where(SocialAccount.brand_id == uuid.UUID(brand["id"]))
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 5
    assert all(r.status == "disconnected" for r in rows)
    assert all(r.zernio_account_id is None for r in rows)


@pytest.mark.asyncio
async def test_webhook_without_origin_gets_401_not_csrf() -> None:
    body = json.dumps(
        {"id": "evt_csrf_1", "type": "account.connected", "data": {"id": "acct_x"}}
    ).encode()
    from httpx import ASGITransport

    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    # No Origin header — CSRF exemption must apply; missing sig → 401 not 403.
    async with AsyncClient(transport=transport, base_url="https://test") as bare:
        resp = await bare.post(
            "/v1/webhooks/zernio/t1",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "UNAUTHORIZED"


@pytest.mark.asyncio
async def test_webhook_dedupe_second_event_enqueues_nothing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    payload = {
        "id": f"evt_dedupe_{uuid.uuid4().hex[:8]}",
        "type": "account.connected",
        "data": {"id": "acct_unused", "platform": "instagram"},
    }
    body = json.dumps(payload).encode()
    sig = hmac.new(_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()

    from httpx import ASGITransport

    from app.main import create_app

    app = create_app()
    transport = ASGITransport(app=app)
    headers = {
        "Content-Type": "application/json",
        "X-Zernio-Signature": sig,
    }
    async with AsyncClient(transport=transport, base_url="https://test") as bare:
        first = await bare.post("/v1/webhooks/zernio/t1", content=body, headers=headers)
        second = await bare.post("/v1/webhooks/zernio/t1", content=body, headers=headers)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text

    await db_session.rollback()
    events = (
        (
            await db_session.execute(
                select(WebhookEvent).where(WebhookEvent.external_event_id == payload["id"])
            )
        )
        .scalars()
        .all()
    )
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

    # Drain is optional — claim should find at most one.
    claimed = await claim(db_session, queue="webhooks", batch_size=10, worker_id="test-webhook")
    matching = [
        j
        for j in claimed
        if j.get("payload", {}).get("webhook_event_id") == events[0].id
        or str(j.get("payload", {}).get("webhook_event_id")) == str(events[0].id)
    ]
    assert len(matching) <= 1


@pytest.mark.asyncio
async def test_capabilities_endpoint(client: AsyncClient, seed_member: SeedMember) -> None:
    _org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    response = await client.get("/v1/channels/capabilities")
    assert response.status_code == 200, response.text
    platforms = {p["platform"]: p for p in response.json()["platforms"]}
    assert platforms["whatsapp"]["publishable"] is False
    assert platforms["whatsapp"]["connectable"] is False
    assert platforms["snapchat"]["connectable"] is False
    assert platforms["snapchat"]["publishable"] is False
    assert platforms["instagram"]["publishable"] is True


@pytest.mark.asyncio
async def test_disconnect_calls_zernio_delete(client: AsyncClient, seed_member: SeedMember) -> None:
    from app.integrations.social import reset_fake_social_provider

    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    fake = reset_fake_social_provider()

    oauth = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/instagram/oauth-url"
    )
    assert oauth.status_code == 200, oauth.text
    channels = (await client.get(f"/v1/orgs/{org_id}/brands/{brand['id']}/channels")).json()
    ig = next(c for c in channels if c["platform"] == "instagram")
    connected = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/{ig['id']}/connect"
    )
    assert connected.status_code == 200, connected.text
    assert connected.json()["status"] == "connected"
    deleted = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/{ig['id']}/disconnect"
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "disconnected"

    delete_calls = [c for c in fake.calls if c[0] == "delete_account"]
    assert len(delete_calls) == 1
    assert delete_calls[0][1]["account_id"]


@pytest.mark.asyncio
async def test_disconnect_fails_when_zernio_delete_unavailable(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    from app.integrations.social import reset_fake_social_provider

    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    fake = reset_fake_social_provider()

    await client.post(f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/instagram/oauth-url")
    channels = (await client.get(f"/v1/orgs/{org_id}/brands/{brand['id']}/channels")).json()
    ig = next(c for c in channels if c["platform"] == "instagram")
    connected = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/{ig['id']}/connect"
    )
    assert connected.json()["status"] == "connected"

    fake.delete_account_script = "unavailable"
    failed = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/{ig['id']}/disconnect"
    )
    assert failed.status_code == 502, failed.text
    assert failed.json()["error"]["code"] == "PROVIDER_UNAVAILABLE"

    still = (await client.get(f"/v1/orgs/{org_id}/brands/{brand['id']}/channels")).json()
    ig_after = next(c for c in still if c["platform"] == "instagram")
    assert ig_after["status"] == "connected"


@pytest.mark.asyncio
async def test_oauth_fails_over_when_key_at_capacity(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    """Brand pinned to a full key gets oauth-url on another key (no disable)."""
    from app.features.social_accounts.models import ZernioCredential, ZernioProfile
    from app.integrations.social import reset_fake_social_provider

    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = uuid.UUID(brand["id"])

    fake = reset_fake_social_provider()

    # First oauth pins a profile (usually least-loaded / fill_to_tier).
    first = await client.post(f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/facebook/oauth-url")
    assert first.status_code == 200, first.text

    await db_session.rollback()
    profile = (
        await db_session.execute(
            select(ZernioProfile).where(
                ZernioProfile.brand_id == brand_id,
                ZernioProfile.deleted_at.is_(None),
            )
        )
    ).scalar_one()
    full_cred = (
        await db_session.execute(
            select(ZernioCredential).where(ZernioCredential.id == profile.credential_id)
        )
    ).scalar_one()
    full_alias = full_cred.alias
    full_cred.connected_accounts = full_cred.max_accounts
    await db_session.commit()

    # Next connect must not disable the full key; fake would still return 200.
    second = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/instagram/oauth-url"
    )
    assert second.status_code == 200, second.text

    await db_session.rollback()
    profiles = (
        (
            await db_session.execute(
                select(ZernioProfile).where(
                    ZernioProfile.brand_id == brand_id,
                    ZernioProfile.deleted_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(profiles) == 2
    aliases = set()
    for p in profiles:
        cred = (
            await db_session.execute(
                select(ZernioCredential).where(ZernioCredential.id == p.credential_id)
            )
        ).scalar_one()
        aliases.add(cred.alias)
    assert full_alias in aliases
    assert len(aliases) == 2

    # Capacity must not hard-disable the original key.
    refreshed = (
        await db_session.execute(
            select(ZernioCredential).where(ZernioCredential.alias == full_alias)
        )
    ).scalar_one()
    assert refreshed.status == "active"

    # Fake recorded two successful connect URLs (no payment script).
    connect_calls = [c for c in fake.calls if c[0] == "get_connect_url"]
    assert len(connect_calls) >= 2


@pytest.mark.asyncio
async def test_oauth_retries_on_free_tier_exceeded_402(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    """402 free_tier_exceeded marks at_capacity and succeeds on the next key."""
    from sqlalchemy import update

    from app.features.social_accounts.models import ZernioCredential
    from app.integrations.social import reset_fake_social_provider

    # Durable test DB may retain elevated connected_accounts from prior runs;
    # this case needs at least two free keys for failover after 402.
    await db_session.execute(
        update(ZernioCredential)
        .where(ZernioCredential.alias.in_(("t1", "t2", "t3")))
        .values(connected_accounts=0, status="active", notes=None)
    )
    await db_session.commit()

    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)

    fake = reset_fake_social_provider()
    fake.connect_url_failures_remaining = 1
    fake.connect_url_fail_reason = "free_tier_exceeded"

    response = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand['id']}/channels/instagram/oauth-url"
    )
    assert response.status_code == 200, response.text

    connect_calls = [c for c in fake.calls if c[0] == "get_connect_url"]
    assert len(connect_calls) == 2

    await db_session.rollback()
    # At least one credential should note at_capacity; none disabled for capacity.
    rows = (await db_session.execute(select(ZernioCredential))).scalars().all()
    assert any(r.notes == "at_capacity" for r in rows)
    assert all(r.status != "disabled" or r.notes != "at_capacity" for r in rows)
    assert not any(r.status == "disabled" and r.notes == "at_capacity" for r in rows)


@pytest.mark.asyncio
async def test_ensure_credentials_upserts_t4(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import get_settings
    from app.features.social_accounts.models import ZernioCredential
    from app.features.social_accounts.repository import ensure_credentials_from_env
    from app.integrations.social import credential_aliases

    monkeypatch.setenv("ZERNIO_CREDENTIAL_ALIASES", "t1,t2,t3,t4")
    monkeypatch.setenv("ZERNIO_API_KEY__t4", "fake-key-t4")
    get_settings.cache_clear()
    settings = get_settings()

    aliases = credential_aliases(settings)
    assert "t4" in aliases

    await ensure_credentials_from_env(
        db_session,
        aliases=aliases,
        max_profiles=settings.social.max_profiles,
        max_accounts=settings.social.max_accounts_per_credential,
    )
    await db_session.commit()

    t4 = (
        await db_session.execute(select(ZernioCredential).where(ZernioCredential.alias == "t4"))
    ).scalar_one()
    assert t4.secret_ref == "ZERNIO_API_KEY__t4"  # noqa: S105
    assert t4.max_accounts == settings.social.max_accounts_per_credential
    assert t4.status == "active"


async def test_ensure_credentials_reactivates_test_only_t4(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Parked test_only keys must revive when listed in ZERNIO_CREDENTIAL_ALIASES."""
    from app.core.config import get_settings
    from app.features.social_accounts.models import ZernioCredential
    from app.features.social_accounts.repository import ensure_credentials_from_env
    from app.integrations.social import credential_aliases

    monkeypatch.setenv("ZERNIO_CREDENTIAL_ALIASES", "t1,t2,t3,t4")
    monkeypatch.setenv("ZERNIO_API_KEY__t4", "fake-key-t4")
    get_settings.cache_clear()
    settings = get_settings()

    await ensure_credentials_from_env(
        db_session,
        aliases=credential_aliases(settings),
        max_profiles=settings.social.max_profiles,
        max_accounts=settings.social.max_accounts_per_credential,
    )
    t4 = (
        await db_session.execute(select(ZernioCredential).where(ZernioCredential.alias == "t4"))
    ).scalar_one()
    t4.status = "disabled"
    t4.notes = "test_only"
    t4.connected_accounts = t4.max_accounts
    await db_session.commit()

    await ensure_credentials_from_env(
        db_session,
        aliases=credential_aliases(settings),
        max_profiles=settings.social.max_profiles,
        max_accounts=settings.social.max_accounts_per_credential,
    )
    await db_session.commit()

    revived = (
        await db_session.execute(select(ZernioCredential).where(ZernioCredential.alias == "t4"))
    ).scalar_one()
    assert revived.status == "active"
    assert revived.notes is None
