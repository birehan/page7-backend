"""Invite accept / team tests. Callers: pytest. Uses signup_and_verify.
API: signup+verify-email, team invite, accept. Schemas: SessionPayload, User.email_verified_at.
User instruction: Implement the plan as specified (Signup email verification 6-digit OTP)."""

from __future__ import annotations

import re
from collections.abc import Callable, Coroutine
from typing import Any

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.auth.models import User
from app.integrations.email.fakes import FakeEmailProvider
from tests.auth_signup_helpers import signup_and_verify


def _extract_invite_token(fake: FakeEmailProvider) -> str:
    message = fake.sent[-1]
    match = re.search(r"invite/([\w-]+)", message.text)
    assert match is not None, message.text
    return match.group(1)


async def _signup(
    client: AsyncClient,
    db_session: AsyncSession,
    email: str,
    org_name: str = "Invite Co",
) -> dict[str, Any]:
    return await signup_and_verify(
        client,
        db_session,
        email=email,
        name="Owner",
        organization_name=org_name,
    )

async def test_invite_creates_a_pending_team_member(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
    db_session: AsyncSession,
) -> None:
    client, fake = client_with_fake_email
    session = await _signup(client, db_session, "invite-owner@example.com")
    org_id = session["organizationId"]

    response = await client.post(
        f"/v1/orgs/{org_id}/team", json={"email": "invitee@example.com", "role": "editor"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["email"] == "invitee@example.com"
    await drain_email()
    assert len(fake.sent) == 1

    team_list = await client.get(f"/v1/orgs/{org_id}/team")
    assert team_list.status_code == 200
    statuses = {m["email"]: m["status"] for m in team_list.json()}
    assert statuses == {"invite-owner@example.com": "active", "invitee@example.com": "pending"}


async def test_invite_email_uses_requested_locale(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
    db_session: AsyncSession,
) -> None:
    """Regression: inviting from /en/settings/team used to send Arabic copy
    because create_invitation defaulted locale to ar and the UI never passed it.
    """
    import uuid

    client, fake = client_with_fake_email
    suffix = uuid.uuid4().hex[:8]
    session = await _signup(client, db_session, f"locale-owner-{suffix}@example.com")
    org_id = session["organizationId"]

    response = await client.post(
        f"/v1/orgs/{org_id}/team",
        json={
            "email": f"locale-invitee-{suffix}@example.com",
            "role": "editor",
            "locale": "en",
        },
    )
    assert response.status_code == 201
    await drain_email()
    assert len(fake.sent) == 1
    message = fake.sent[-1]
    assert "You're invited to join" in message.subject
    assert "/en/invite/" in message.text
    assert "Accept invitation" in message.html
    assert "قبول الدعوة" not in message.html


async def test_invite_allows_an_existing_user_from_another_org(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
    db_session: AsyncSession,
) -> None:
    """Regression: inviting an email that already owns another org used to
    409 EMAIL_IN_USE because create_invitation checked global users, not
    this org's memberships. Callers: pytest. User: "invite note working, fix it"
    """
    client, fake = client_with_fake_email
    await _signup(client, db_session, "existing-invitee@example.com", "Invitee Co")
    await client.post("/v1/auth/logout")

    owner = await _signup(client, db_session, "cross-org-owner@example.com", "Owner Co")
    org_id = owner["organizationId"]

    response = await client.post(
        f"/v1/orgs/{org_id}/team",
        json={"email": "existing-invitee@example.com", "role": "editor"},
    )

    assert response.status_code == 201
    assert response.json()["status"] == "pending"
    await drain_email()
    assert len(fake.sent) == 1


async def test_editor_cannot_invite_a_member(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
    db_session: AsyncSession,
) -> None:
    client, fake = client_with_fake_email
    owner_session = await _signup(client, db_session, "editor-owner@example.com")
    org_id = owner_session["organizationId"]
    await client.post(
        f"/v1/orgs/{org_id}/team", json={"email": "editor-user@example.com", "role": "editor"}
    )
    await drain_email()
    token = _extract_invite_token(fake)
    await client.post("/v1/auth/logout")
    await client.post(
        f"/v1/auth/invite/{token}/accept",
        json={"name": "Editor User", "password": "editor password 123"},
    )

    response = await client.post(
        f"/v1/orgs/{org_id}/team", json={"email": "someone-else@example.com", "role": "viewer"}
    )

    assert response.status_code == 403


async def test_invite_preview_shows_org_and_role_without_authentication(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
    db_session: AsyncSession,
) -> None:
    client, fake = client_with_fake_email
    session = await _signup(client, db_session, "preview-owner@example.com", "Preview Co")
    org_id = session["organizationId"]
    await client.post(
        f"/v1/orgs/{org_id}/team", json={"email": "preview-invitee@example.com", "role": "admin"}
    )
    await drain_email()
    token = _extract_invite_token(fake)
    await client.post("/v1/auth/logout")

    preview = await client.get(f"/v1/auth/invite/{token}")

    assert preview.status_code == 200
    body = preview.json()
    assert body["organizationName"] == "Preview Co"
    assert body["email"] == "preview-invitee@example.com"
    assert body["role"] == "admin"
    assert body["inviterName"] == "Owner"


async def test_accept_invite_creates_a_member_and_signs_them_in(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
    db_session: AsyncSession,
) -> None:
    client, fake = client_with_fake_email
    session = await _signup(client, db_session, "accept-owner@example.com", "Accept Co")
    org_id = session["organizationId"]
    await client.post(
        f"/v1/orgs/{org_id}/team", json={"email": "accept-invitee@example.com", "role": "editor"}
    )
    await drain_email()
    token = _extract_invite_token(fake)
    await client.post("/v1/auth/logout")

    accept = await client.post(
        f"/v1/auth/invite/{token}/accept",
        json={"name": "Invitee Name", "password": "invitee password 123"},
    )

    assert accept.status_code == 200
    body = accept.json()
    assert body["user"]["email"] == "accept-invitee@example.com"
    assert body["user"]["role"] == "editor"
    assert body["organizationId"] == org_id

    invitee = (
        await db_session.execute(select(User).where(User.email == "accept-invitee@example.com"))
    ).scalar_one()
    assert invitee.email_verified_at is not None

    team_list = await client.get(f"/v1/orgs/{org_id}/team")
    statuses = {m["email"]: m["status"] for m in team_list.json()}
    assert statuses["accept-invitee@example.com"] == "active"


async def test_accept_invite_joins_an_existing_user_to_the_org(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
    db_session: AsyncSession,
) -> None:
    client, fake = client_with_fake_email
    await _signup(client, db_session, "join-existing@example.com", "Existing Co")
    await client.post("/v1/auth/logout")

    owner = await _signup(client, db_session, "join-owner@example.com", "Join Co")
    org_id = owner["organizationId"]
    await client.post(
        f"/v1/orgs/{org_id}/team", json={"email": "join-existing@example.com", "role": "viewer"}
    )
    await drain_email()
    token = _extract_invite_token(fake)
    await client.post("/v1/auth/logout")

    accept = await client.post(
        f"/v1/auth/invite/{token}/accept",
        json={"name": "Ignored", "password": "correct horse battery staple"},
    )

    assert accept.status_code == 200
    body = accept.json()
    assert body["user"]["email"] == "join-existing@example.com"
    assert body["user"]["role"] == "viewer"
    assert body["organizationId"] == org_id

    team_list = await client.get(f"/v1/orgs/{org_id}/team")
    statuses = {m["email"]: m["status"] for m in team_list.json()}
    assert statuses["join-existing@example.com"] == "active"


async def test_accept_invite_twice_fails(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
    db_session: AsyncSession,
) -> None:
    client, fake = client_with_fake_email
    session = await _signup(client, db_session, "double-accept-owner@example.com")
    org_id = session["organizationId"]
    await client.post(
        f"/v1/orgs/{org_id}/team", json={"email": "double-accept@example.com", "role": "editor"}
    )
    await drain_email()
    token = _extract_invite_token(fake)
    await client.post("/v1/auth/logout")

    first = await client.post(
        f"/v1/auth/invite/{token}/accept", json={"name": "Name", "password": "password 12345"}
    )
    assert first.status_code == 200

    second = await client.post(
        f"/v1/auth/invite/{token}/accept", json={"name": "Name", "password": "password 12345"}
    )
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "INVALID_TOKEN"


async def test_update_member_role_and_remove_member(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
    db_session: AsyncSession,
) -> None:
    client, fake = client_with_fake_email
    session = await _signup(client, db_session, "manage-owner@example.com")
    org_id = session["organizationId"]
    await client.post(
        f"/v1/orgs/{org_id}/team", json={"email": "manage-invitee@example.com", "role": "editor"}
    )
    await drain_email()
    token = _extract_invite_token(fake)

    # Accept via a fresh client-equivalent flow (same client, logs itself in
    # over the owner's session — acceptable for this test, whoever holds the
    # cookie last is who subsequent requests act as).
    accept = await client.post(
        f"/v1/auth/invite/{token}/accept",
        json={"name": "Manage User", "password": "password 12345"},
    )
    member_id = accept.json()["user"]["id"]

    # Switch back to the owner to manage the new member.
    await client.post("/v1/auth/logout")
    await client.post(
        "/v1/auth/login",
        json={"email": "manage-owner@example.com", "password": "correct horse battery staple"},
    )

    role_change = await client.patch(f"/v1/orgs/{org_id}/team/{member_id}", json={"role": "admin"})
    assert role_change.status_code == 200
    assert role_change.json()["role"] == "admin"

    remove = await client.delete(f"/v1/orgs/{org_id}/team/{member_id}")
    assert remove.status_code == 204

    team_list = await client.get(f"/v1/orgs/{org_id}/team")
    emails = {m["email"] for m in team_list.json()}
    assert "manage-invitee@example.com" not in emails


async def test_resend_invite_sends_a_new_email(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
    db_session: AsyncSession,
) -> None:
    client, fake = client_with_fake_email
    session = await _signup(client, db_session, "resend-owner@example.com")
    org_id = session["organizationId"]
    invite = await client.post(
        f"/v1/orgs/{org_id}/team", json={"email": "resend-invitee@example.com", "role": "viewer"}
    )
    invite_id = invite.json()["id"]
    await drain_email()
    assert len(fake.sent) == 1

    resend = await client.post(f"/v1/orgs/{org_id}/team/{invite_id}/resend-invite")

    assert resend.status_code == 200
    await drain_email()
    assert len(fake.sent) == 2


async def test_revoked_invite_cannot_be_accepted(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
    db_session: AsyncSession,
) -> None:
    client, fake = client_with_fake_email
    session = await _signup(client, db_session, "revoke-owner@example.com")
    org_id = session["organizationId"]
    invite = await client.post(
        f"/v1/orgs/{org_id}/team", json={"email": "revoke-invitee@example.com", "role": "viewer"}
    )
    invite_id = invite.json()["id"]
    await drain_email()
    token = _extract_invite_token(fake)

    revoke = await client.delete(f"/v1/orgs/{org_id}/team/{invite_id}")
    assert revoke.status_code == 204

    await client.post("/v1/auth/logout")
    accept = await client.post(
        f"/v1/auth/invite/{token}/accept", json={"name": "Name", "password": "password 12345"}
    )
    assert accept.status_code == 400
    assert accept.json()["error"]["code"] == "INVALID_TOKEN"
