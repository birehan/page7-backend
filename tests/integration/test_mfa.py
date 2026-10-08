"""MFA integration tests. Callers: pytest. Uses signup_and_verify helper.
API: /auth/signup+verify-email, /auth/mfa/*, /auth/login. Instruction: OTP signup plan."""

from __future__ import annotations

from datetime import UTC, datetime

import pyotp
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.auth_signup_helpers import signup_and_verify


async def _signup(client: AsyncClient, db_session: AsyncSession, email: str) -> None:
    await signup_and_verify(
        client,
        db_session,
        email=email,
        name="MFA User",
        organization_name="MFA Co",
    )


async def test_mfa_enroll_and_verify_enables_mfa(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signup(client, db_session, "mfa-enroll@example.com")

    enroll = await client.post("/v1/auth/mfa/enroll")
    assert enroll.status_code == 200
    body = enroll.json()
    assert len(body["recoveryCodes"]) == 10
    secret = body["secret"]

    code = pyotp.TOTP(secret).now()
    verify = await client.post("/v1/auth/mfa/verify", json={"code": code})
    assert verify.status_code == 200
    assert verify.json()["mfaEnabled"] is True

    me = await client.get("/v1/auth/me")
    assert me.json()["mfaEnabled"] is True


async def test_login_for_mfa_enabled_account_returns_a_challenge(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signup(client, db_session, "mfa-login@example.com")
    secret = (await client.post("/v1/auth/mfa/enroll")).json()["secret"]
    code = pyotp.TOTP(secret).now()
    await client.post("/v1/auth/mfa/verify", json={"code": code})
    await client.post("/v1/auth/logout")

    login = await client.post(
        "/v1/auth/login",
        json={"email": "mfa-login@example.com", "password": "correct horse battery staple"},
    )
    assert login.status_code == 200
    body = login.json()
    assert body["status"] == "mfa_required"
    assert "challengeToken" in body

    # No cookie is set until the challenge is completed.
    assert "pgblank_session" not in login.cookies


async def test_mfa_challenge_completes_login_and_sets_the_cookie(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signup(client, db_session, "mfa-challenge@example.com")
    secret = (await client.post("/v1/auth/mfa/enroll")).json()["secret"]
    code = pyotp.TOTP(secret).now()
    await client.post("/v1/auth/mfa/verify", json={"code": code})
    await client.post("/v1/auth/logout")

    login = await client.post(
        "/v1/auth/login",
        json={"email": "mfa-challenge@example.com", "password": "correct horse battery staple"},
    )
    challenge_token = login.json()["challengeToken"]

    # `.now()` again can return the exact same code as the enrollment step
    # above if both calls land in the same 30s window — deliberately request
    # the *next* step's code instead of relying on wall-clock timing.
    totp = pyotp.TOTP(secret)
    next_code = totp.generate_otp(totp.timecode(datetime.now(UTC)) + 1)
    verify = await client.post(
        "/v1/auth/mfa/verify", json={"code": next_code, "challengeToken": challenge_token}
    )
    assert verify.status_code == 200
    assert verify.json()["user"]["email"] == "mfa-challenge@example.com"

    me = await client.get("/v1/auth/me")
    assert me.status_code == 200


async def test_a_replayed_totp_code_is_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _signup(client, db_session, "mfa-replay@example.com")
    secret = (await client.post("/v1/auth/mfa/enroll")).json()["secret"]
    code = pyotp.TOTP(secret).now()
    await client.post("/v1/auth/mfa/verify", json={"code": code})
    await client.post("/v1/auth/logout")

    login = await client.post(
        "/v1/auth/login",
        json={"email": "mfa-replay@example.com", "password": "correct horse battery staple"},
    )
    challenge_token = login.json()["challengeToken"]

    # Same code used for enrollment confirmation, replayed against the login
    # challenge — architecture/05 §2's replay protection must reject it even
    # though it's a code that would otherwise validate.
    replayed = await client.post(
        "/v1/auth/mfa/verify", json={"code": code, "challengeToken": challenge_token}
    )
    assert replayed.status_code == 401
    assert replayed.json()["error"]["code"] == "INVALID_CREDENTIALS"
