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


async def test_liveness_and_readiness_have_a_stable_status_field(client: AsyncClient) -> None:
    for path in ("/health/live", "/health/ready"):
        response = await client.get(path)
        assert response.status_code == 200
        assert response.json()["status"] == "ok"


async def test_deep_health_always_reports_a_known_status_and_its_checks(
    client: AsyncClient,
) -> None:
    """Deep health reflects live system state (queue lag, scheduler), so it may legitimately
    be 503. What must stay stable is the shape monitors parse: the status vocabulary, a
    status code that agrees with it, and a `checks` object with the database result."""
    response = await client.get("/health/deep")
    body = response.json()
    assert body["status"] in {"ok", "degraded", "down"}
    assert response.status_code == (200 if body["status"] == "ok" else 503)
    assert isinstance(body["checks"], dict)
    assert "ok" in body["checks"]["database"]
