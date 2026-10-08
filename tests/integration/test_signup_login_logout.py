"""Signup / login / logout / email verification integration tests.

Importers/callers: pytest integration suite; uses tests.auth_signup_helpers.
API: POST /v1/auth/signup, /verify-email, /resend-verification, /login, /logout, /me.
Schemas: EmailVerificationRequiredOut, SessionPayloadOut, User.email_verified_at,
EmailVerificationChallenge.
User instruction: Implement the plan as specified (Signup email verification 6-digit OTP).
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import utc_now
from app.features.audit.models import AuditLog
from app.features.auth.models import EmailVerificationChallenge, User
from app.integrations.email.fakes import FakeEmailProvider
from tests.auth_signup_helpers import (
    DEFAULT_PASSWORD,
    claim_latest_verify_code,
    extract_otp_code,
    signup_and_verify,
)


async def test_signup_requires_email_verification_without_session_cookie(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await client.post(
        "/v1/auth/signup",
        json={
            "name": "Founder",
            "email": "founder@example.com",
            "password": DEFAULT_PASSWORD,
            "organizationName": "Acme Co",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "email_verification_required"
    assert "challengeToken" in body
    assert body["email"].endswith("@example.com")
    assert get_settings().auth.session_cookie_name not in response.cookies

    user = (
        await db_session.execute(select(User).where(User.email == "founder@example.com"))
    ).scalar_one()
    assert user.password_hash is not None
    assert user.email_verified_at is None

    code = await claim_latest_verify_code(db_session)
    verify = await client.post(
        "/v1/auth/verify-email",
        json={"challengeToken": body["challengeToken"], "code": code},
    )
    assert verify.status_code == 200
    session = verify.json()
    assert session["user"]["email"] == "founder@example.com"
    assert session["user"]["role"] == "owner"
    assert session["organizationName"] == "Acme Co"
    assert get_settings().auth.session_cookie_name in verify.cookies

    await db_session.refresh(user)
    assert user.email_verified_at is not None

    audit_rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.organization_id == session["organizationId"]
                )
            )
        )
        .scalars()
        .all()
    )
    assert [row.action for row in audit_rows] == ["org.created"]


async def test_signup_rejects_duplicate_verified_email(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await signup_and_verify(client, db_session, email="dup@example.com")
    await client.post("/v1/auth/logout")

    second = await client.post(
        "/v1/auth/signup",
        json={
            "name": "Founder",
            "email": "dup@example.com",
            "password": DEFAULT_PASSWORD,
            "organizationName": "Acme Co",
        },
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "EMAIL_IN_USE"


async def test_signup_unverified_same_password_rotates_challenge(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    body = {
        "name": "Founder",
        "email": "orphan@example.com",
        "password": DEFAULT_PASSWORD,
        "organizationName": "Acme Co",
    }
    first = await client.post("/v1/auth/signup", json=body)
    assert first.status_code == 200
    first_token = first.json()["challengeToken"]
    await claim_latest_verify_code(db_session)

    second = await client.post("/v1/auth/signup", json=body)
    assert second.status_code == 200
    assert second.json()["status"] == "email_verification_required"
    assert second.json()["challengeToken"] != first_token


async def test_verify_email_rejects_wrong_code_and_exhausts(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    signup = await client.post(
        "/v1/auth/signup",
        json={
            "name": "Wrong Code",
            "email": "wrong-code@example.com",
            "password": DEFAULT_PASSWORD,
            "organizationName": "Wrong Co",
        },
    )
    token = signup.json()["challengeToken"]
    await claim_latest_verify_code(db_session)

    for _ in range(4):
        bad = await client.post(
            "/v1/auth/verify-email",
            json={"challengeToken": token, "code": "000000"},
        )
        assert bad.status_code == 400
        assert bad.json()["error"]["code"] == "INVALID_CODE"

    exhausted = await client.post(
        "/v1/auth/verify-email",
        json={"challengeToken": token, "code": "000000"},
    )
    assert exhausted.status_code == 400
    assert exhausted.json()["error"]["code"] == "CODE_EXHAUSTED"


async def test_verify_email_rejects_expired_challenge(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    signup = await client.post(
        "/v1/auth/signup",
        json={
            "name": "Expired",
            "email": "expired@example.com",
            "password": DEFAULT_PASSWORD,
            "organizationName": "Expired Co",
        },
    )
    token = signup.json()["challengeToken"]
    code = await claim_latest_verify_code(db_session)

    user_id = (
        await db_session.execute(select(User.id).where(User.email == "expired@example.com"))
    ).scalar_one()
    challenge = (
        await db_session.execute(
            select(EmailVerificationChallenge).where(
                EmailVerificationChallenge.user_id == user_id,
                EmailVerificationChallenge.consumed_at.is_(None),
            )
        )
    ).scalar_one()
    challenge.expires_at = utc_now() - timedelta(seconds=1)
    await db_session.commit()

    response = await client.post(
        "/v1/auth/verify-email",
        json={"challengeToken": token, "code": code},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "INVALID_TOKEN"


async def test_resend_verification_enforces_cooldown(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    signup = await client.post(
        "/v1/auth/signup",
        json={
            "name": "Resend",
            "email": "resend@example.com",
            "password": DEFAULT_PASSWORD,
            "organizationName": "Resend Co",
        },
    )
    token = signup.json()["challengeToken"]
    await claim_latest_verify_code(db_session)

    too_soon = await client.post(
        "/v1/auth/resend-verification",
        json={"challengeToken": token},
    )
    assert too_soon.status_code == 429
    assert too_soon.json()["error"]["code"] == "RESEND_TOO_SOON"


async def test_resend_verification_issues_new_code_after_cooldown(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
    db_session: AsyncSession,
) -> None:
    client, fake = client_with_fake_email
    signup = await client.post(
        "/v1/auth/signup",
        json={
            "name": "Resend Ok",
            "email": "resend-ok@example.com",
            "password": DEFAULT_PASSWORD,
            "organizationName": "Resend Ok Co",
        },
    )
    token = signup.json()["challengeToken"]
    await drain_email()
    first_code = extract_otp_code(fake)

    user_id = (
        await db_session.execute(select(User.id).where(User.email == "resend-ok@example.com"))
    ).scalar_one()
    challenge = (
        await db_session.execute(
            select(EmailVerificationChallenge).where(
                EmailVerificationChallenge.user_id == user_id,
                EmailVerificationChallenge.consumed_at.is_(None),
            )
        )
    ).scalar_one()
    challenge.created_at = utc_now() - timedelta(seconds=61)
    await db_session.commit()

    resend = await client.post(
        "/v1/auth/resend-verification",
        json={"challengeToken": token},
    )
    assert resend.status_code == 200
    new_token = resend.json()["challengeToken"]
    assert new_token != token

    await drain_email()
    second_code = extract_otp_code(fake)
    assert second_code != first_code

    verify = await client.post(
        "/v1/auth/verify-email",
        json={"challengeToken": new_token, "code": second_code},
    )
    assert verify.status_code == 200


async def test_login_while_unverified_returns_email_verification_required(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    signup = await client.post(
        "/v1/auth/signup",
        json={
            "name": "Unverified Login",
            "email": "unverified-login@example.com",
            "password": DEFAULT_PASSWORD,
            "organizationName": "Unverified Co",
        },
    )
    assert signup.json()["status"] == "email_verification_required"
    await claim_latest_verify_code(db_session)

    login = await client.post(
        "/v1/auth/login",
        json={"email": "unverified-login@example.com", "password": DEFAULT_PASSWORD},
    )
    assert login.status_code == 200
    body = login.json()
    assert body["status"] == "email_verification_required"
    assert "challengeToken" in body
    assert get_settings().auth.session_cookie_name not in login.cookies
    await claim_latest_verify_code(db_session)


async def test_login_with_correct_password_returns_authenticated_session(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await signup_and_verify(client, db_session, email="login-user@example.com")
    await client.post("/v1/auth/logout")

    response = await client.post(
        "/v1/auth/login",
        json={"email": "login-user@example.com", "password": DEFAULT_PASSWORD},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "authenticated"
    assert body["session"]["user"]["email"] == "login-user@example.com"


async def test_login_with_wrong_password_is_invalid_credentials(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await signup_and_verify(client, db_session, email="wrong-pw@example.com")
    await client.post("/v1/auth/logout")

    response = await client.post(
        "/v1/auth/login", json={"email": "wrong-pw@example.com", "password": "nope nope nope"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_login_for_unknown_email_is_also_invalid_credentials(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/login",
        json={"email": "does-not-exist@example.com", "password": "whatever12345"},
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_get_me_reflects_the_signed_up_user(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await signup_and_verify(client, db_session, email="me-user@example.com")

    me = await client.get("/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "me-user@example.com"


async def test_logout_revokes_the_session(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await signup_and_verify(client, db_session, email="logout-user@example.com")
    assert (await client.get("/v1/auth/me")).status_code == 200

    logout = await client.post("/v1/auth/logout")
    assert logout.status_code == 204

    after = await client.get("/v1/auth/me")
    assert after.status_code == 401


async def test_get_me_without_a_session_is_unauthorized(client: AsyncClient) -> None:
    response = await client.get("/v1/auth/me")
    assert response.status_code == 401
