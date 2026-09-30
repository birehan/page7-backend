from __future__ import annotations

import re
from collections.abc import Callable, Coroutine
from typing import Any

from httpx import AsyncClient

from app.integrations.email.fakes import FakeEmailProvider


def _extract_invite_token(fake: FakeEmailProvider) -> str:
    message = fake.sent[-1]
    match = re.search(r"invite/([\w-]+)", message.text)
    assert match is not None, message.text
    return match.group(1)


async def _signup(client: AsyncClient, email: str, org_name: str = "Invite Co") -> dict[str, Any]:
    response = await client.post(
        "/v1/auth/signup",
        json={
            "name": "Owner",
            "email": email,
            "password": "correct horse battery staple",
            "organizationName": org_name,
        },
    )
    assert response.status_code == 200
    return response.json()  # type: ignore[no-any-return]


async def test_invite_creates_a_pending_team_member(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
) -> None:
    client, fake = client_with_fake_email
    session = await _signup(client, "invite-owner@example.com")
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


async def test_editor_cannot_invite_a_member(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
) -> None:
    client, fake = client_with_fake_email
    owner_session = await _signup(client, "editor-owner@example.com")
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
) -> None:
    client, fake = client_with_fake_email
    session = await _signup(client, "preview-owner@example.com", "Preview Co")
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
) -> None:
    client, fake = client_with_fake_email
    session = await _signup(client, "accept-owner@example.com", "Accept Co")
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

    team_list = await client.get(f"/v1/orgs/{org_id}/team")
    statuses = {m["email"]: m["status"] for m in team_list.json()}
    assert statuses["accept-invitee@example.com"] == "active"


async def test_accept_invite_twice_fails(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: Callable[[], Coroutine[Any, Any, None]],
) -> None:
    client, fake = client_with_fake_email
    session = await _signup(client, "double-accept-owner@example.com")
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
) -> None:
    client, fake = client_with_fake_email
    session = await _signup(client, "manage-owner@example.com")
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
) -> None:
    client, fake = client_with_fake_email
    session = await _signup(client, "resend-owner@example.com")
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
) -> None:
    client, fake = client_with_fake_email
    session = await _signup(client, "revoke-owner@example.com")
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
