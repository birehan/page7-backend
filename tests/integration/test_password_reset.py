from __future__ import annotations

import re
from collections.abc import Callable, Coroutine
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.auth.models import Session as AuthSession
from app.features.auth.models import User
from app.integrations.email.fakes import FakeEmailProvider


def _extract_token(fake: FakeEmailProvider, *, path_segment: str) -> str:
    assert len(fake.sent) >= 1
    match = re.search(rf"{path_segment}/([\w-]+)", fake.sent[-1].text)
    assert match is not None, fake.sent[-1].text
    return match.group(1)


async def _signup(client: AsyncClient, email: str) -> None:
    response = await client.post(
        "/v1/auth/signup",
        json={
            "name": "Reset User",
            "email": email,
            "password": "correct horse battery staple",
            "organizationName": "Reset Co",
        },
    )
    assert response.status_code == 200


async def test_forgot_password_for_a_known_email_sends_a_reset_email(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
) -> None:
    client, fake = client_with_fake_email
    await _signup(client, "forgot-known@example.com")

    response = await client.post("/v1/auth/forgot", json={"email": "forgot-known@example.com"})

    assert response.status_code == 204
    await drain_email()
    assert len(fake.sent) == 1
    assert fake.sent[0].to == "forgot-known@example.com"


async def test_forgot_password_for_an_unknown_email_sends_nothing_but_still_204(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
) -> None:
    client, fake = client_with_fake_email

    response = await client.post("/v1/auth/forgot", json={"email": "does-not-exist@example.com"})

    assert response.status_code == 204
    assert fake.sent == []


async def test_reset_password_with_a_bad_token_is_invalid_token(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/reset", json={"token": "not-a-real-token", "password": "new password 123"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


async def test_reset_password_end_to_end(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
    db_session: AsyncSession,
) -> None:
    client, fake = client_with_fake_email
    await _signup(client, "reset-e2e@example.com")
    user = (
        await db_session.execute(select(User).where(User.email == "reset-e2e@example.com"))
    ).scalar_one()
    live_before = (
        (
            await db_session.execute(
                select(AuthSession).where(
                    AuthSession.user_id == user.id, AuthSession.revoked_at.is_(None)
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(live_before) == 1

    await client.post("/v1/auth/forgot", json={"email": "reset-e2e@example.com"})
    # Drain the queue so the email handler runs and fake.sent is populated.
    await drain_email()
    token = _extract_token(fake, path_segment="reset-password")

    reset = await client.post(
        "/v1/auth/reset", json={"token": token, "password": "a brand new password 123"}
    )
    assert reset.status_code == 204

    # architecture/05 §1: a password change revokes every live session.
    await db_session.refresh(live_before[0])
    assert live_before[0].revoked_at is not None

    old_password_login = await client.post(
        "/v1/auth/login",
        json={"email": "reset-e2e@example.com", "password": "correct horse battery staple"},
    )
    assert old_password_login.status_code == 401

    new_password_login = await client.post(
        "/v1/auth/login",
        json={"email": "reset-e2e@example.com", "password": "a brand new password 123"},
    )
    assert new_password_login.status_code == 200


async def test_reset_token_is_single_use(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
) -> None:
    client, fake = client_with_fake_email
    await _signup(client, "reset-single-use@example.com")
    await client.post("/v1/auth/forgot", json={"email": "reset-single-use@example.com"})
    await drain_email()
    token = _extract_token(fake, path_segment="reset-password")

    first = await client.post(
        "/v1/auth/reset", json={"token": token, "password": "first new password 123"}
    )
    assert first.status_code == 204

    second = await client.post(
        "/v1/auth/reset", json={"token": token, "password": "second new password 123"}
    )
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "INVALID_TOKEN"
