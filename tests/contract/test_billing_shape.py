"""Contract tests for Phase 13 billing endpoints against api-contract.json."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import jsonschema
import pytest
from httpx import AsyncClient

from app.core.config import get_settings
from tests.contract.conftest import find_endpoint
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


def _validate(instance: Any, schema: dict[str, Any] | None) -> None:
    if schema is None:
        return
    jsonschema.validate(instance=instance, schema=schema)


@pytest.mark.asyncio
async def test_billing_endpoints_match_the_frontend_contract(
    client: AsyncClient,
    seed_member: SeedMember,
    api_contract: dict[str, Any],
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    get_sub = find_endpoint(api_contract, "billing", "GET", path_contains="subscription")
    sub = await client.get(f"/v1/orgs/{org_id}/billing/subscription")
    assert sub.status_code == 200, sub.text
    _validate(sub.json(), get_sub["response"])

    get_inv = find_endpoint(api_contract, "billing", "GET", path_contains="invoices")
    invoices = await client.get(f"/v1/orgs/{org_id}/billing/invoices")
    assert invoices.status_code == 200, invoices.text
    _validate(invoices.json(), get_inv["response"])

    patch_sub = find_endpoint(
        api_contract, "billing", "PATCH", path_contains="subscription"
    )
    patched = await client.patch(
        f"/v1/orgs/{org_id}/billing/subscription",
        json={"plan": "Agency"},
        headers={"Idempotency-Key": f"billing-contract:{uuid.uuid4()}"},
    )
    assert patched.status_code == 200, patched.text
    _validate(patched.json(), patch_sub["response"])

    get_usage = find_endpoint(api_contract, "billing", "GET", path_contains="ai-usage")
    since = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    usage = await client.get(
        f"/v1/orgs/{org_id}/billing/ai-usage",
        params={"since": since.isoformat().replace("+00:00", "Z")},
    )
    assert usage.status_code == 200, usage.text
    _validate(usage.json(), get_usage["response"])
