"""Phase 2 Definition of Done, verbatim: "signup -> invite -> accept -> login
with MFA -> freeze -> audit visible" succeeds end to end against a real
Postgres, exercised as an API-level test with no mock in the loop. Asserts
both HTTP responses and DB state after each step, using `FakeEmailProvider`
to capture the invite token rather than parsing a real inbox.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pyotp
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.audit.models import AuditLog
from app.features.organizations.models import OrgSettings
from app.features.team.models import Membership
from app.integrations.email.fakes import FakeEmailProvider


async def test_full_journey(
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],
    drain_email: object,  # Callable[[], Coroutine[Any, Any, None]]
    db_session: AsyncSession,
) -> None:
    from collections.abc import Callable, Coroutine
    from typing import Any
    _drain: Callable[[], Coroutine[Any, Any, None]] = drain_email  # type: ignore[assignment]

    client, fake = client_with_fake_email

    # --- signup ---
    signup = await client.post(
        "/v1/auth/signup",
        json={
            "name": "Journey Owner",
            "email": "journey-owner@example.com",
            "password": "correct horse battery staple",
            "organizationName": "Journey Co",
        },
    )
    assert signup.status_code == 200
    session = signup.json()
    org_id = session["organizationId"]
    assert session["user"]["role"] == "owner"

    owner_memberships = (
        (await db_session.execute(select(Membership).where(Membership.organization_id == org_id)))
        .scalars()
        .all()
    )
    assert len(owner_memberships) == 1
    assert owner_memberships[0].role == "owner"

    # --- invite ---
    invite = await client.post(
        f"/v1/orgs/{org_id}/team", json={"email": "journey-editor@example.com", "role": "editor"}
    )
    assert invite.status_code == 201
    assert invite.json()["status"] == "pending"
    # Phase 3: email is enqueued; drain the queue before checking fake.sent.
    await _drain()
    assert len(fake.sent) == 1
    assert fake.sent[0].to == "journey-editor@example.com"

    match = re.search(r"invite/([\w-]+)", fake.sent[0].text)
    assert match is not None
    invite_token = match.group(1)

    # --- accept ---
    await client.post("/v1/auth/logout")
    accept = await client.post(
        f"/v1/auth/invite/{invite_token}/accept",
        json={"name": "Journey Editor", "password": "editor password 123"},
    )
    assert accept.status_code == 200
    assert accept.json()["user"]["role"] == "editor"

    memberships_after_accept = (
        (await db_session.execute(select(Membership).where(Membership.organization_id == org_id)))
        .scalars()
        .all()
    )
    assert {m.role for m in memberships_after_accept} == {"owner", "editor"}

    # --- login with MFA (back to the owner) ---
    await client.post("/v1/auth/logout")
    login = await client.post(
        "/v1/auth/login",
        json={"email": "journey-owner@example.com", "password": "correct horse battery staple"},
    )
    assert login.status_code == 200
    assert login.json()["status"] == "authenticated"

    enroll = await client.post("/v1/auth/mfa/enroll")
    assert enroll.status_code == 200
    secret = enroll.json()["secret"]
    confirm = await client.post("/v1/auth/mfa/verify", json={"code": pyotp.TOTP(secret).now()})
    assert confirm.status_code == 200
    assert confirm.json()["mfaEnabled"] is True

    await client.post("/v1/auth/logout")
    mfa_login = await client.post(
        "/v1/auth/login",
        json={"email": "journey-owner@example.com", "password": "correct horse battery staple"},
    )
    assert mfa_login.status_code == 200
    assert mfa_login.json()["status"] == "mfa_required"
    challenge_token = mfa_login.json()["challengeToken"]

    totp = pyotp.TOTP(secret)
    next_code = totp.generate_otp(totp.timecode(datetime.now(UTC)) + 1)
    verify = await client.post(
        "/v1/auth/mfa/verify", json={"code": next_code, "challengeToken": challenge_token}
    )
    assert verify.status_code == 200
    assert verify.json()["user"]["email"] == "journey-owner@example.com"

    # --- freeze ---
    freeze = await client.post(
        f"/v1/orgs/{org_id}/settings/freeze", json={"reason": "journey test"}
    )
    assert freeze.status_code == 200
    assert freeze.json()["publishingFrozen"] is True

    settings_row = await db_session.get(OrgSettings, org_id)
    assert settings_row is not None
    assert settings_row.publishing_frozen is True

    # --- audit visible ---
    audit = await client.get(f"/v1/orgs/{org_id}/audit")
    assert audit.status_code == 200
    actions = {item["action"] for item in audit.json()["items"]}
    assert {"org.created", "team.invited", "team.invite_accepted", "publishing.frozen"} <= actions

    audit_rows = (
        (await db_session.execute(select(AuditLog).where(AuditLog.organization_id == org_id)))
        .scalars()
        .all()
    )
    assert len(audit_rows) >= 4
