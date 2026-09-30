from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.config import CORSSettings, Settings
from app.core.security.csrf import CSRFMiddleware

ALLOWED = "https://app.pgblank.ai"


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)

    @app.post("/mutate")
    async def mutate() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/read")
    async def read() -> dict[str, bool]:
        return {"ok": True}

    return app


@pytest.fixture
async def csrf_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[AsyncClient]:
    settings = Settings(cors=CORSSettings(allowed_origins=[ALLOWED]), _env_file=None)
    monkeypatch.setattr("app.core.security.csrf.get_settings", lambda: settings)
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="https://test") as client:
        yield client


async def test_safe_methods_bypass_the_origin_check(csrf_client: AsyncClient) -> None:
    response = await csrf_client.get("/read")
    assert response.status_code == 200


async def test_mutation_with_the_allowed_origin_succeeds(csrf_client: AsyncClient) -> None:
    response = await csrf_client.post("/mutate", headers={"Origin": ALLOWED})
    assert response.status_code == 200


async def test_mutation_with_no_origin_or_referer_is_rejected(csrf_client: AsyncClient) -> None:
    response = await csrf_client.post("/mutate")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_mutation_with_a_disallowed_origin_is_rejected(csrf_client: AsyncClient) -> None:
    response = await csrf_client.post("/mutate", headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


async def test_mutation_falls_back_to_referer_when_origin_is_absent(
    csrf_client: AsyncClient,
) -> None:
    response = await csrf_client.post(
        "/mutate", headers={"Referer": f"{ALLOWED}/settings/security"}
    )
    assert response.status_code == 200
