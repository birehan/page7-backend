from __future__ import annotations

from httpx import AsyncClient


async def test_liveness_never_touches_the_database(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reaches_the_database(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_deep_health_checks_nothing_yet(client: AsyncClient) -> None:
    # No provider credential exists in-app until Phase 2's Resend integration.
    response = await client.get("/health/deep")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {}}
