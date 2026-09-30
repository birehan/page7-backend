from __future__ import annotations

from typing import Any

import jsonschema
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.audit import record
from tests.db_fixtures import SeedMember


def _find_endpoint(api_contract: dict[str, Any], resource: str, method: str) -> dict[str, Any]:
    endpoints: list[dict[str, Any]] = api_contract["endpoints"]
    for endpoint in endpoints:
        if endpoint["resource"] == resource and endpoint["method"] == method:
            return endpoint
    raise AssertionError(f"no {method} endpoint for resource {resource!r} in the contract")


async def test_audit_list_response_matches_the_frontend_contract(
    client: AsyncClient,
    db_session: AsyncSession,
    seed_member: SeedMember,
    api_contract: dict[str, Any],
) -> None:
    org_id, user_id, raw_token = await seed_member(role="admin")
    await record(
        db_session,
        organization_id=org_id,
        actor_kind="user",
        actor_ref=str(user_id),
        actor_name="Seeded User",
        action="org.created",
        target_type="organization",
        target_id=org_id,
    )
    await db_session.commit()
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)

    response = await client.get(f"/v1/orgs/{org_id}/audit")

    assert response.status_code == 200
    endpoint = _find_endpoint(api_contract, "audit", "GET")
    jsonschema.validate(instance=response.json(), schema=endpoint["response"])
