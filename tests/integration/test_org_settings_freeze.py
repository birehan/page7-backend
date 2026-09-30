from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.audit.models import AuditLog
from app.features.organizations.models import OrgSettings
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def test_get_settings_returns_defaults_for_a_fresh_org(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="admin")
    _login(client, raw_token)

    response = await client.get(f"/v1/orgs/{org_id}/settings")

    assert response.status_code == 200
    body = response.json()
    assert body["publishingFrozen"] is False
    assert body["approvalPolicy"] == {"mode": "always", "riskThreshold": 0.15}
    assert body["onboarding"] == {
        "brand": False,
        "channel": False,
        "plan": False,
        "firstApproval": False,
    }
    assert "frozenBy" not in body


async def test_editor_cannot_freeze_publishing(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)

    response = await client.post(f"/v1/orgs/{org_id}/settings/freeze", json={})
    assert response.status_code == 403


async def test_admin_can_freeze_and_unfreeze_publishing(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, user_id, raw_token = await seed_member(role="admin")
    _login(client, raw_token)

    freeze_response = await client.post(
        f"/v1/orgs/{org_id}/settings/freeze", json={"reason": "incident"}
    )
    assert freeze_response.status_code == 200
    body = freeze_response.json()
    assert body["publishingFrozen"] is True
    assert body["frozenBy"] == "Seeded User"
    assert body["frozenReason"] == "incident"

    reloaded = await db_session.get(OrgSettings, org_id)
    assert reloaded is not None
    assert reloaded.publishing_frozen is True
    assert reloaded.frozen_by_user_id == user_id

    unfreeze_response = await client.post(f"/v1/orgs/{org_id}/settings/unfreeze")
    assert unfreeze_response.status_code == 200
    unfreeze_body = unfreeze_response.json()
    assert unfreeze_body["publishingFrozen"] is False
    assert "frozenBy" not in unfreeze_body


async def test_freeze_is_audited(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    await client.post(f"/v1/orgs/{org_id}/settings/freeze", json={"reason": "test"})

    rows = (
        (
            await db_session.execute(
                select(AuditLog).where(
                    AuditLog.organization_id == org_id, AuditLog.action == "publishing.frozen"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_generic_patch_cannot_set_freeze_fields(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    response = await client.patch(f"/v1/orgs/{org_id}/settings", json={"publishingFrozen": True})
    assert response.status_code == 422
