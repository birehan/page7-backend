from __future__ import annotations

from typing import Any

import jsonschema
from httpx import AsyncClient

from app.core.config import get_settings
from tests.contract.conftest import find_endpoint
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def test_brand_endpoints_match_the_frontend_contract(
    client: AsyncClient, seed_member: SeedMember, api_contract: dict[str, Any]
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    created = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={"name": "Contract Brand", "industry": "retail", "city": "Riyadh"},
    )
    assert created.status_code == 201
    brand = created.json()
    jsonschema.validate(
        instance=brand,
        schema=find_endpoint(api_contract, "brands", "POST")["response"],
    )

    listed = await client.get(f"/v1/orgs/{org_id}/brands")
    assert listed.status_code == 200
    jsonschema.validate(
        instance=listed.json(),
        schema=find_endpoint(api_contract, "brands", "GET", path_contains="/brands")[
            "response"
        ],
    )

    got = await client.get(f"/v1/orgs/{org_id}/brands/{brand['id']}")
    assert got.status_code == 200
    jsonschema.validate(
        instance=got.json(),
        schema=find_endpoint(
            api_contract, "brands", "GET", path_contains="/brands/:brandId"
        )["response"],
    )

    generated = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand['id']}/generate",
        json={"name": "Contract Brand", "industry": "retail", "city": "Riyadh"},
        headers={"Idempotency-Key": f"brand-generate:{brand['id']}"},
    )
    assert generated.status_code == 200
    jsonschema.validate(
        instance=generated.json(),
        schema=find_endpoint(api_contract, "brands", "POST", path_contains="/generate")[
            "response"
        ],
    )

    patched = await client.patch(
        f"/v1/orgs/{org_id}/brands/{brand['id']}",
        json=generated.json(),
    )
    assert patched.status_code == 200
    jsonschema.validate(
        instance=patched.json(),
        schema=find_endpoint(api_contract, "brands", "PATCH")["response"],
    )

    strategy = await client.get(f"/v1/orgs/{org_id}/brands/{brand['id']}/strategy")
    assert strategy.status_code == 200
    jsonschema.validate(
        instance=strategy.json(),
        schema=find_endpoint(api_contract, "strategy", "GET")["response"],
    )

    events = await client.get(f"/v1/orgs/{org_id}/cultural-events")
    assert events.status_code == 200
    jsonschema.validate(
        instance=events.json(),
        schema=find_endpoint(api_contract, "culturalCalendar", "GET")["response"],
    )
    if events.json():
        toggled = await client.post(
            f"/v1/orgs/{org_id}/cultural-events/{events.json()[0]['id']}/toggle"
        )
        assert toggled.status_code == 200
        jsonschema.validate(
            instance=toggled.json(),
            schema=find_endpoint(api_contract, "culturalCalendar", "POST")["response"],
        )

    deleted = await client.delete(f"/v1/orgs/{org_id}/brands/{brand['id']}")
    assert deleted.status_code == 204
