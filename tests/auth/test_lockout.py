from __future__ import annotations

from collections.abc import AsyncIterator

import pyotp
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.auth_signup_helpers import signup_and_verify


@pytest.fixture(autouse=True)
async def _isolate_rate_limits(db_session: AsyncSession) -> AsyncIterator[None]:
    """These tests deliberately drive a bucket to its lockout threshold.
    Every test under `ASGITransport` shares the same synthetic client IP
    ("127.0.0.1"), so without cleanup, driving `login:ip:127.0.0.1` to 11
    here would spuriously 429 any *other* test's login call that runs
    afterward in the same session — including ones in a completely
    different file. Clean up both before and after, so this file is
    self-contained regardless of run order.
    """
    await db_session.execute(text("TRUNCATE TABLE rate_limits"))
    await db_session.commit()
    yield
    await db_session.execute(text("TRUNCATE TABLE rate_limits"))
    await db_session.commit()


async def test_login_lockout_after_ten_failures(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await signup_and_verify(
        client,
        db_session,
        email="lockout-login@example.com",
        name="Lockout User",
        organization_name="Lockout Co",
    )
    await client.post("/v1/auth/logout")

    responses = [
        await client.post(
            "/v1/auth/login",
            json={"email": "lockout-login@example.com", "password": "wrong password"},
        )
        for _ in range(11)
    ]

    for response in responses[:10]:
        assert response.status_code == 401
    assert responses[10].status_code == 429
    assert responses[10].json()["error"]["code"] == "RATE_LIMIT"


async def test_mfa_verify_lockout_after_five_failures(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await signup_and_verify(
        client,
        db_session,
        email="lockout-mfa@example.com",
        name="MFA Lockout User",
        organization_name="MFA Lockout Co",
    )
    secret = (await client.post("/v1/auth/mfa/enroll")).json()["secret"]
    code = pyotp.TOTP(secret).now()
    await client.post("/v1/auth/mfa/verify", json={"code": code})
    await client.post("/v1/auth/logout")

    login = await client.post(
        "/v1/auth/login",
        json={"email": "lockout-mfa@example.com", "password": "correct horse battery staple"},
    )
    challenge_token = login.json()["challengeToken"]

    responses = [
        await client.post(
            "/v1/auth/mfa/verify", json={"code": "000000", "challengeToken": challenge_token}
        )
        for _ in range(6)
    ]

    for response in responses[:5]:
        assert response.status_code == 401
    assert responses[5].status_code == 429
    assert responses[5].json()["error"]["code"] == "RATE_LIMIT"
