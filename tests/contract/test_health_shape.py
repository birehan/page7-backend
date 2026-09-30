from __future__ import annotations

from typing import Any

from httpx import AsyncClient


async def test_health_is_intentionally_absent_from_the_shared_frontend_contract(
    api_contract: dict[str, Any],
) -> None:
    """The frontend never calls /health/* — it's an infra-level concern (load
    balancer / container orchestrator), not part of the client's API surface. This
    test documents that omission is deliberate rather than a gap someone should
    "fix" by adding a health resource to the frontend registry.
    """
    resources = {endpoint["resource"] for endpoint in api_contract["endpoints"]}
    assert "health" not in resources


async def test_health_responses_have_a_stable_status_field(client: AsyncClient) -> None:
    for path in ("/health/live", "/health/ready", "/health/deep"):
        response = await client.get(path)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
