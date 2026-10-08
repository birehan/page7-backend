"""Google OIDC auth tests.

Callers: pytest via `just test` / auth suite.
API: GET /auth/providers, /auth/google/start, /auth/google/callback.
User instruction: Implement the plan as specified (Page7 auth + Google OIDC).
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Iterator
from urllib.parse import parse_qs, urlparse

import pyotp
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.auth_signup_helpers import signup_and_verify

from app.core.config import AuthSettings, Settings, get_settings
from app.features.auth import google as google_client
from app.features.auth.google import GoogleProfile
from app.features.auth.models import User, UserIdentity
from app.main import create_app

_auth_router_mod = importlib.import_module("app.features.auth.router")


def _uid() -> str:
    return uuid.uuid4().hex[:10]


@pytest.fixture
def google_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    get_settings.cache_clear()
    base = get_settings()
    auth = AuthSettings(
        session_cookie_name=base.auth.session_cookie_name,
        frontend_url=base.auth.frontend_url,
        cookie_samesite=base.auth.cookie_samesite,
        google_client_id="test-google-client-id",
        google_client_secret=SecretStr("test-google-client-secret"),
        google_redirect_uri="https://test/v1/auth/google/callback",
    )
    patched = base.model_copy(update={"auth": auth})

    def _settings() -> Settings:
        return patched

    monkeypatch.setattr("app.core.config.get_settings", _settings)
    monkeypatch.setattr(_auth_router_mod, "get_settings", _settings)
    monkeypatch.setattr("app.core.security.csrf.get_settings", _settings)
    yield patched


def _client() -> AsyncClient:
    settings = get_settings()
    return AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="https://test",
        headers={"Origin": settings.cors.allowed_origins[0]},
        follow_redirects=False,
    )


async def _start_and_extract_state(client: AsyncClient) -> str:
    start = await client.get("/v1/auth/google/start", params={"locale": "en"})
    assert start.status_code == 200, start.text
    url = start.json()["authorizeUrl"]
    qs = parse_qs(urlparse(url).query)
    state = qs["state"][0]
    assert isinstance(state, str)
    return state


@pytest.mark.usefixtures("google_settings")
async def test_providers_reports_google_when_configured(client: AsyncClient) -> None:
    async with _client() as ac:
        resp = await ac.get("/v1/auth/providers")
    assert resp.status_code == 200
    assert resp.json() == {"google": True}


async def test_providers_reports_google_off_by_default(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient
) -> None:
    # This test asserts the *unconfigured* case specifically — it must not
    # depend on the ambient environment happening to have no
    # AUTH__GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI set (a real backend/.env used
    # for local Google sign-in development sets exactly these, which would
    # otherwise make this test fail outside of a "clean" environment even
    # though the product behavior it's checking is unaffected). Force the
    # unconfigured state explicitly, mirroring how `google_settings` above
    # explicitly forces the configured state rather than relying on env
    # absence/presence either way.
    get_settings.cache_clear()
    base = get_settings()
    auth = AuthSettings(
        session_cookie_name=base.auth.session_cookie_name,
        frontend_url=base.auth.frontend_url,
        cookie_samesite=base.auth.cookie_samesite,
        google_client_id=None,
        google_client_secret=None,
        google_redirect_uri=None,
    )
    patched = base.model_copy(update={"auth": auth})
    monkeypatch.setattr("app.core.config.get_settings", lambda: patched)
    monkeypatch.setattr(_auth_router_mod, "get_settings", lambda: patched)
    monkeypatch.setattr("app.core.security.csrf.get_settings", lambda: patched)

    async with _client() as ac:
        resp = await ac.get("/v1/auth/providers")
    assert resp.status_code == 200
    assert resp.json()["google"] is False


@pytest.mark.usefixtures("google_settings")
async def test_google_start_returns_authorize_url() -> None:
    async with _client() as ac:
        resp = await ac.get("/v1/auth/google/start", params={"rememberMe": "true", "locale": "ar"})
    assert resp.status_code == 200
    url = resp.json()["authorizeUrl"]
    parsed = urlparse(url)
    assert parsed.netloc == "accounts.google.com"
    qs = parse_qs(parsed.query)
    assert qs["client_id"] == ["test-google-client-id"]
    assert "state" in qs
    assert "nonce" in qs


@pytest.mark.usefixtures("google_settings")
async def test_google_callback_invalid_state_redirects_to_login() -> None:
    async with _client() as ac:
        resp = await ac.get(
            "/v1/auth/google/callback",
            params={"code": "x", "state": "bogus"},
        )
    assert resp.status_code == 302
    assert "/login" in resp.headers["location"]
    assert "error=google" in resp.headers["location"]


@pytest.mark.usefixtures("google_settings")
async def test_google_new_user_creates_org_and_session(
    monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    email = f"google-new-{_uid()}@example.com"
    subject = f"google-sub-new-{_uid()}"

    async def fake_exchange(code: str, *, settings: AuthSettings) -> GoogleProfile:
        assert code == "good-code"
        return GoogleProfile(
            subject=subject,
            email=email,
            email_verified=True,
            name="Sara Alharbi",
            picture="https://lh3.googleusercontent.com/a/test",
        )

    monkeypatch.setattr(google_client, "exchange_code", fake_exchange)

    async with _client() as ac:
        state = await _start_and_extract_state(ac)
        resp = await ac.get(
            "/v1/auth/google/callback",
            params={"code": "good-code", "state": state},
        )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "/onboarding" in location
    assert get_settings().auth.session_cookie_name in resp.cookies

    user = (await db_session.execute(select(User).where(User.email == email))).scalar_one()
    assert user.password_hash is None
    assert user.email_verified_at is not None
    identity = (
        await db_session.execute(
            select(UserIdentity).where(UserIdentity.provider_subject == subject)
        )
    ).scalar_one()
    assert identity.user_id == user.id
    assert user.name == "Sara Alharbi"


@pytest.mark.usefixtures("google_settings")
async def test_google_returning_identity_goes_to_dashboard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = 0
    email = f"google-return-{_uid()}@example.com"
    subject = f"google-sub-return-{_uid()}"

    async def fake_exchange(code: str, *, settings: AuthSettings) -> GoogleProfile:
        nonlocal call_count
        call_count += 1
        return GoogleProfile(
            subject=subject,
            email=email,
            email_verified=True,
            name="Return User",
            picture=None,
        )

    monkeypatch.setattr(google_client, "exchange_code", fake_exchange)

    async with _client() as ac:
        state1 = await _start_and_extract_state(ac)
        first = await ac.get(
            "/v1/auth/google/callback",
            params={"code": "c1", "state": state1},
        )
        assert "/onboarding" in first.headers["location"]

        state2 = await _start_and_extract_state(ac)
        second = await ac.get(
            "/v1/auth/google/callback",
            params={"code": "c2", "state": state2},
        )
    assert second.status_code == 302
    assert "/dashboard" in second.headers["location"]
    assert call_count == 2


@pytest.mark.usefixtures("google_settings")
async def test_google_links_by_verified_email(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient, db_session: AsyncSession
) -> None:
    email = f"link-me-{_uid()}@example.com"
    subject = f"google-sub-link-{_uid()}"
    signup = await signup_and_verify(
        client,
        db_session,
        email=email,
        name="Existing User",
        organization_name="Link Co",
    )
    existing_id = signup["user"]["id"]

    async def fake_exchange(code: str, *, settings: AuthSettings) -> GoogleProfile:
        return GoogleProfile(
            subject=subject,
            email=email,
            email_verified=True,
            name="Existing User",
            picture=None,
        )

    monkeypatch.setattr(google_client, "exchange_code", fake_exchange)

    async with _client() as ac:
        state = await _start_and_extract_state(ac)
        resp = await ac.get(
            "/v1/auth/google/callback",
            params={"code": "link", "state": state},
        )
    assert resp.status_code == 302
    assert "/dashboard" in resp.headers["location"]

    identity = (
        await db_session.execute(
            select(UserIdentity).where(UserIdentity.provider_subject == subject)
        )
    ).scalar_one()
    assert str(identity.user_id) == existing_id


@pytest.mark.usefixtures("google_settings")
async def test_google_mfa_redirects_with_challenge(
    monkeypatch: pytest.MonkeyPatch, client: AsyncClient, db_session: AsyncSession
) -> None:
    email = f"google-mfa-{_uid()}@example.com"
    subject = f"google-sub-mfa-{_uid()}"
    await signup_and_verify(
        client,
        db_session,
        email=email,
        name="MFA User",
        organization_name="MFA Co",
    )
    enroll = await client.post("/v1/auth/mfa/enroll")
    assert enroll.status_code == 200
    secret = enroll.json()["secret"]
    code = pyotp.TOTP(secret).now()
    verify = await client.post("/v1/auth/mfa/verify", json={"code": code})
    assert verify.status_code == 200

    async def fake_exchange(code: str, *, settings: AuthSettings) -> GoogleProfile:
        return GoogleProfile(
            subject=subject,
            email=email,
            email_verified=True,
            name="MFA User",
            picture=None,
        )

    monkeypatch.setattr(google_client, "exchange_code", fake_exchange)

    async with _client() as ac:
        state = await _start_and_extract_state(ac)
        resp = await ac.get(
            "/v1/auth/google/callback",
            params={"code": "mfa", "state": state},
        )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "/login" in location
    assert "mfaChallenge=" in location
    assert get_settings().auth.session_cookie_name not in resp.cookies
