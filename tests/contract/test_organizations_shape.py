from __future__ import annotations

from typing import Any

import jsonschema
from httpx import AsyncClient

from app.core.config import get_settings
from tests.db_fixtures import SeedMember


def _find_endpoint(api_contract: dict[str, Any], resource: str, method: str) -> dict[str, Any]:
    endpoints: list[dict[str, Any]] = api_contract["endpoints"]
    for endpoint in endpoints:
        if endpoint["resource"] == resource and endpoint["method"] == method:
            return endpoint
    raise AssertionError(f"no {method} endpoint for resource {resource!r} in the contract")


async def test_get_organization_matches_the_frontend_contract(
    client: AsyncClient, seed_member: SeedMember, api_contract: dict[str, Any]
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="admin")
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)

    response = await client.get(f"/v1/orgs/{org_id}")

    assert response.status_code == 200
    endpoint = _find_endpoint(api_contract, "organizations", "GET")
    jsonschema.validate(instance=response.json(), schema=endpoint["response"])


async def test_get_org_settings_matches_the_frontend_contract(
    client: AsyncClient, seed_member: SeedMember, api_contract: dict[str, Any]
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="admin")
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)

    response = await client.get(f"/v1/orgs/{org_id}/settings")

    assert response.status_code == 200
    endpoint = _find_endpoint(api_contract, "orgSettings", "GET")
    jsonschema.validate(instance=response.json(), schema=endpoint["response"])


async def test_freeze_response_matches_the_frontend_contract(
    client: AsyncClient, seed_member: SeedMember, api_contract: dict[str, Any]
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)

    response = await client.post(f"/v1/orgs/{org_id}/settings/freeze", json={"reason": "test"})

    assert response.status_code == 200
    endpoint = _find_endpoint(api_contract, "orgSettings", "POST")
    # Two POST entries share this resource (freeze, unfreeze); both share the
    # same response schema, so either match validates this response.
    jsonschema.validate(instance=response.json(), schema=endpoint["response"])
