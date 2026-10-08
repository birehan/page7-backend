from __future__ import annotations

import re

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.auth.models import Session as AuthSession
from app.integrations.email import get_email_provider
from app.integrations.email.fakes import FakeEmailProvider
from app.main import create_app
from tests.auth_signup_helpers import signup_and_verify


def _client_for(raw_token: str | None = None) -> AsyncClient:
    settings = get_settings()
    cookies = {settings.auth.session_cookie_name: raw_token} if raw_token else {}
    return AsyncClient(
        transport=ASGITransport(app=create_app()),
        base_url="https://test",
        headers={"Origin": settings.cors.allowed_origins[0]},
        cookies=cookies,
    )


async def test_logout_revokes_exactly_one_session_row(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    signup = await signup_and_verify(
        client,
        db_session,
        email="session-lifecycle@example.com",
        name="Session User",
        organization_name="Session Co",
    )
    user_id = signup["user"]["id"]

    async with _client_for() as second:
        await second.post(
            "/v1/auth/login",
            json={
                "email": "session-lifecycle@example.com",
                "password": "correct horse battery staple",
            },
        )

    await client.post("/v1/auth/logout")

    sessions = (
        (await db_session.execute(select(AuthSession).where(AuthSession.user_id == user_id)))
        .scalars()
        .all()
    )
    assert len(sessions) == 2
    revoked = [s for s in sessions if s.revoked_at is not None]
    live = [s for s in sessions if s.revoked_at is None]
    assert len(revoked) == 1
    assert len(live) == 1


async def test_password_reset_revokes_every_session_not_just_one(
    db_session: AsyncSession,
) -> None:
    # Builds its own app/override rather than depending on
    # `client_with_fake_email`, since this test also needs a second,
    # independent client sharing the same app instance concurrently.
    import app.jobs.handlers.email_send as _email_handler
    from tests.job_test_helpers import drain_email_jobs

    app = create_app()
    fake = FakeEmailProvider()
    app.dependency_overrides[get_email_provider] = lambda: fake
    # Phase 3: the handler calls get_email_provider directly (not via DI).
    _original_fn = _email_handler.get_email_provider  # type: ignore[attr-defined]
    _email_handler.get_email_provider = lambda _s: fake  # type: ignore[attr-defined, assignment]
    settings = get_settings()

    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="https://test",
            headers={"Origin": settings.cors.allowed_origins[0]},
        ) as client_a:
            signup = await signup_and_verify(
                client_a,
                db_session,
                email="reset-lifecycle@example.com",
                name="Reset Lifecycle User",
                organization_name="Reset Lifecycle Co",
            )
            user_id = signup["user"]["id"]

            async with AsyncClient(
                transport=ASGITransport(app=app),
                base_url="https://test",
                headers={"Origin": settings.cors.allowed_origins[0]},
            ) as client_b:
                await client_b.post(
                    "/v1/auth/login",
                    json={
                        "email": "reset-lifecycle@example.com",
                        "password": "correct horse battery staple",
                    },
                )

            await client_a.post("/v1/auth/forgot", json={"email": "reset-lifecycle@example.com"})
            # Drain the email job inline (Phase 3: sends happen via the queue).
            await drain_email_jobs(db_session, fake)
            match = re.search(r"reset-password/([\w-]+)", fake.sent[-1].text)
            assert match is not None
            token = match.group(1)

            await client_a.post(
                "/v1/auth/reset", json={"token": token, "password": "brand new password 123"}
            )
    finally:
        _email_handler.get_email_provider = _original_fn  # type: ignore[attr-defined]

    sessions = (
        (await db_session.execute(select(AuthSession).where(AuthSession.user_id == user_id)))
        .scalars()
        .all()
    )
    assert len(sessions) == 2
    assert all(s.revoked_at is not None for s in sessions)


async def test_remember_me_session_gets_a_thirty_day_expiry(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    signup = await signup_and_verify(
        client,
        db_session,
        email="remember-me@example.com",
        name="Remember User",
        organization_name="Remember Co",
    )
    signup_expires_at = signup["expiresAt"]
    await client.post("/v1/auth/logout")

    login = await client.post(
        "/v1/auth/login",
        json={
            "email": "remember-me@example.com",
            "password": "correct horse battery staple",
            "rememberMe": True,
        },
    )
    body = login.json()
    remember_expires_at = body["session"]["expiresAt"]

    # Signup's default session is the 12h sliding window; remember_me is 30
    # days fixed — the gap between the two must be on the order of weeks, not
    # hours, or `rememberMe` isn't actually doing anything.
    assert (remember_expires_at - signup_expires_at) > (20 * 24 * 60 * 60 * 1000)
