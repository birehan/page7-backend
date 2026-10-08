from __future__ import annotations

import time

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.auth_signup_helpers import signup_and_verify

# Generous — this only needs to catch a gross timing leak (e.g. skipping the
# email send entirely on a miss), not prove indistinguishability under
# adversarial statistical analysis. See auth/service.py's own note on this
# being an approximate mitigation, not a formal guarantee.
_TIMING_TOLERANCE_SECONDS = 2.0


async def test_forgot_password_response_is_identical_for_known_and_unknown_email(
    client: AsyncClient, db_session: AsyncSession,
) -> None:
    await signup_and_verify(
        client,
        db_session,
        email="enum-known@example.com",
        name="Enum User",
        organization_name="Enum Co",
    )

    known = await client.post("/v1/auth/forgot", json={"email": "enum-known@example.com"})
    unknown = await client.post("/v1/auth/forgot", json={"email": "enum-unknown@example.com"})

    assert known.status_code == unknown.status_code == 204
    assert known.text == unknown.text


async def test_forgot_password_timing_is_comparable_for_known_and_unknown_email(
    client: AsyncClient, db_session: AsyncSession,
) -> None:
    await signup_and_verify(
        client,
        db_session,
        email="timing-known@example.com",
        name="Timing User",
        organization_name="Timing Co",
    )

    start = time.monotonic()
    await client.post("/v1/auth/forgot", json={"email": "timing-known@example.com"})
    known_elapsed = time.monotonic() - start

    start = time.monotonic()
    await client.post("/v1/auth/forgot", json={"email": "timing-unknown@example.com"})
    unknown_elapsed = time.monotonic() - start

    assert abs(known_elapsed - unknown_elapsed) < _TIMING_TOLERANCE_SECONDS


async def test_login_error_is_identical_for_wrong_password_and_unknown_email(
    client: AsyncClient, db_session: AsyncSession,
) -> None:
    await signup_and_verify(
        client,
        db_session,
        email="login-enum-known@example.com",
        name="Login Enum User",
        organization_name="Login Enum Co",
    )
    await client.post("/v1/auth/logout")

    wrong_password = await client.post(
        "/v1/auth/login",
        json={"email": "login-enum-known@example.com", "password": "totally wrong"},
    )
    unknown_email = await client.post(
        "/v1/auth/login",
        json={"email": "login-enum-unknown@example.com", "password": "totally wrong"},
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json()["error"]["code"] == unknown_email.json()["error"]["code"]
    assert wrong_password.json()["error"]["message"] == unknown_email.json()["error"]["message"]


async def test_login_timing_is_comparable_for_wrong_password_and_unknown_email(
    client: AsyncClient, db_session: AsyncSession,
) -> None:
    await signup_and_verify(
        client,
        db_session,
        email="login-timing-known@example.com",
        name="Login Timing User",
        organization_name="Login Timing Co",
    )
    await client.post("/v1/auth/logout")

    start = time.monotonic()
    await client.post(
        "/v1/auth/login",
        json={"email": "login-timing-known@example.com", "password": "totally wrong"},
    )
    known_elapsed = time.monotonic() - start

    start = time.monotonic()
    await client.post(
        "/v1/auth/login",
        json={"email": "login-timing-unknown@example.com", "password": "totally wrong"},
    )
    unknown_elapsed = time.monotonic() - start

    # architecture/05 §1: the Argon2 hasher runs on both the wrong-password
    # and no-such-user paths (auth/service.py's DUMMY_PASSWORD_HASH verify) —
    # this is what the timing comparison is actually checking.
    assert abs(known_elapsed - unknown_elapsed) < _TIMING_TOLERANCE_SECONDS
