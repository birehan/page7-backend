from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.organizations.models import Organization
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def test_get_organization_returns_the_profile(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="admin")
    _login(client, raw_token)

    response = await client.get(f"/v1/orgs/{org_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(org_id)
    assert body["name"] == "Seeded Org"


async def test_editor_cannot_read_the_organization_profile(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)

    response = await client.get(f"/v1/orgs/{org_id}")
    assert response.status_code == 403


async def test_update_organization_persists_across_connections(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    response = await client.patch(
        f"/v1/orgs/{org_id}", json={"name": "Renamed Org", "industry": "Retail"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed Org"
    assert body["industry"] == "Retail"

    # Proves the write actually committed (get_db_session's fix) — a fresh
    # session on a different connection must see it too.
    reloaded = await db_session.get(Organization, org_id)
    assert reloaded is not None
    assert reloaded.name == "Renamed Org"


async def test_update_organization_normalizes_empty_vat_number_to_null(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    response = await client.patch(f"/v1/orgs/{org_id}", json={"vatNumber": ""})

    assert response.status_code == 200
    assert "vatNumber" not in response.json()

    reloaded = await db_session.get(Organization, org_id)
    assert reloaded is not None
    assert reloaded.vat_number is None


async def test_update_organization_rejects_a_bad_vat_number(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    response = await client.patch(f"/v1/orgs/{org_id}", json={"vatNumber": "not-15-digits"})

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION"
