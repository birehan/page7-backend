"""Password-reset requests and sign-ups are rate limited, without leaking who is registered."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
async def _isolate_rate_limits(db_session: AsyncSession) -> AsyncIterator[None]:
    """Every test under ASGITransport shares one synthetic client IP, so these tests must
    not leave a nearly-full bucket behind for other files (same approach as test_lockout)."""
    await db_session.execute(text("TRUNCATE TABLE rate_limits"))
    await db_session.commit()
    yield
    await db_session.execute(text("TRUNCATE TABLE rate_limits"))
    await db_session.commit()


def _email(tag: str) -> str:
    # example.com, not .test: the email validator rejects reserved special-use domains.
    return f"{tag}-{uuid.uuid4().hex[:10]}@example.com"


async def test_forgot_password_is_limited_per_email(client: AsyncClient) -> None:
    email = _email("forgot")
    codes = [
        (await client.post("/v1/auth/forgot", json={"email": email})).status_code
        for _ in range(4)
    ]
    assert codes == [204, 204, 204, 429]


async def test_the_per_email_limit_does_not_block_other_emails(client: AsyncClient) -> None:
    blocked = _email("blocked")
    for _ in range(4):
        await client.post("/v1/auth/forgot", json={"email": blocked})
    other = await client.post("/v1/auth/forgot", json={"email": _email("other")})
    assert other.status_code == 204


async def test_forgot_password_is_limited_per_ip(client: AsyncClient) -> None:
    codes = [
        (await client.post("/v1/auth/forgot", json={"email": _email("ip")})).status_code
        for _ in range(11)
    ]
    assert codes[:10] == [204] * 10
    assert codes[10] == 429


async def test_signup_is_limited_per_ip(client: AsyncClient) -> None:
    def body() -> dict[str, str]:
        return {
            "name": "Abuse Test",
            "email": _email("signup"),
            "password": "correct horse battery staple",
            "organizationName": "Abuse Co",
            "locale": "en",
        }

    codes = [(await client.post("/v1/auth/signup", json=body())).status_code for _ in range(11)]
    assert codes[:10] == [200] * 10
    assert codes[10] == 429
