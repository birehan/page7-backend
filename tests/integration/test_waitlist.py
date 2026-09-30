"""Pre-launch waitlist — public, unauthenticated join.

Discovered by pytest under tests/integration/ (via tests/conftest.py `client`
fixture). No prior waitlist test file exists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
async def _isolate_waitlist(db_session: AsyncSession) -> AsyncIterator[None]:
    await db_session.execute(text("TRUNCATE TABLE waitlist_signups, rate_limits"))
    await db_session.commit()
    yield
    await db_session.execute(text("TRUNCATE TABLE waitlist_signups, rate_limits"))
    await db_session.commit()


async def test_join_waitlist_creates_signup(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/waitlist",
        json={"email": "owner@cafe.sa", "locale": "en", "source": "hero"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == "owner@cafe.sa"
    assert body["alreadyJoined"] is False
    assert "id" in body
    assert "createdAt" in body


async def test_join_waitlist_is_idempotent(client: AsyncClient) -> None:
    first = await client.post(
        "/v1/waitlist",
        json={"email": "Owner@Cafe.SA", "locale": "ar", "source": "footer_cta"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["alreadyJoined"] is False

    second = await client.post(
        "/v1/waitlist",
        json={"email": "owner@cafe.sa", "locale": "en", "source": "hero"},
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["alreadyJoined"] is True
    assert body["id"] == first.json()["id"]


async def test_join_waitlist_rejects_invalid_email(client: AsyncClient) -> None:
    response = await client.post("/v1/waitlist", json={"email": "not-an-email"})
    assert response.status_code == 422
