from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.audit.models import AuditLog
from app.features.auth.models import User


async def test_signup_creates_org_membership_session_and_returns_cookie(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    response = await client.post(
        "/v1/auth/signup",
        json={
            "name": "Founder",
            "email": "founder@example.com",
            "password": "correct horse battery staple",
            "organizationName": "Acme Co",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["email"] == "founder@example.com"
    assert body["user"]["role"] == "owner"
    assert body["organizationName"] == "Acme Co"
    assert "brandId" not in body
    assert get_settings().auth.session_cookie_name in response.cookies

    user = (
        await db_session.execute(select(User).where(User.email == "founder@example.com"))
    ).scalar_one()
    assert user.password_hash is not None
    assert user.password_hash != "correct horse battery staple"  # noqa: S105 - test fixture, not a secret

    audit_rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.organization_id == uuid.UUID(body["organizationId"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert [row.action for row in audit_rows] == ["org.created"]


async def test_signup_rejects_a_duplicate_email(client: AsyncClient) -> None:
    body = {
        "name": "Founder",
        "email": "dup@example.com",
        "password": "correct horse battery staple",
        "organizationName": "Acme Co",
    }
    first = await client.post("/v1/auth/signup", json=body)
    assert first.status_code == 200

    second = await client.post("/v1/auth/signup", json=body)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "EMAIL_IN_USE"


async def test_login_with_correct_password_returns_authenticated_session(
    client: AsyncClient,
) -> None:
    await client.post(
        "/v1/auth/signup",
        json={
            "name": "Login User",
            "email": "login-user@example.com",
            "password": "correct horse battery staple",
            "organizationName": "Login Co",
        },
    )

    response = await client.post(
        "/v1/auth/login",
        json={"email": "login-user@example.com", "password": "correct horse battery staple"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "authenticated"
    assert body["session"]["user"]["email"] == "login-user@example.com"


async def test_login_with_wrong_password_is_invalid_credentials(client: AsyncClient) -> None:
    await client.post(
        "/v1/auth/signup",
        json={
            "name": "Wrong Pw User",
            "email": "wrong-pw@example.com",
            "password": "correct horse battery staple",
            "organizationName": "Wrong Pw Co",
        },
    )

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


async def test_get_me_reflects_the_signed_up_user(client: AsyncClient) -> None:
    signup = await client.post(
        "/v1/auth/signup",
        json={
            "name": "Me User",
            "email": "me-user@example.com",
            "password": "correct horse battery staple",
            "organizationName": "Me Co",
        },
    )
    assert signup.status_code == 200

    me = await client.get("/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "me-user@example.com"


async def test_logout_revokes_the_session(client: AsyncClient) -> None:
    await client.post(
        "/v1/auth/signup",
        json={
            "name": "Logout User",
            "email": "logout-user@example.com",
            "password": "correct horse battery staple",
            "organizationName": "Logout Co",
        },
    )
    assert (await client.get("/v1/auth/me")).status_code == 200

    logout = await client.post("/v1/auth/logout")
    assert logout.status_code == 204

    after = await client.get("/v1/auth/me")
    assert after.status_code == 401


async def test_get_me_without_a_session_is_unauthorized(client: AsyncClient) -> None:
    response = await client.get("/v1/auth/me")
    assert response.status_code == 401
