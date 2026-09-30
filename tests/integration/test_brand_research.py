"""Phase 8 brand research integration tests — real Postgres, FakeLLM, mocked fetch."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.integrations.webfetch.extract import PageExtraction
from app.integrations.webfetch.fetcher import SiteFetchResult
from app.jobs.handlers.brand_research import handle as handle_brand_research
from app.jobs.queue import claim
from app.sse.models import Run
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


def _parse_sse(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in raw.split("\n\n"):
        data_lines = [
            line.split(":", 1)[1].strip() for line in block.split("\n") if line.startswith("data:")
        ]
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        if payload == "[DONE]":
            continue
        events.append(json.loads(payload))
    return events


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Research Brand {uuid.uuid4().hex[:6]}",
            "industry": "healthcare",
            "city": "Riyadh",
            "website": "https://example-clinic.sa/",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_relearn_streams_proposal(
    client: AsyncClient,
    seed_member: SeedMember,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy full path (fetch + search) still works when RESEARCH__APPROACH=full."""
    monkeypatch.setenv("RESEARCH__APPROACH", "full")
    get_settings.cache_clear()
    try:
        org_id, _user_id, raw_token = await seed_member(role="owner")
        _login(client, raw_token)
        brand = await _create_brand(client, org_id)

        site = SiteFetchResult(
            source_url="https://example-clinic.sa/",
            pages=[
                PageExtraction(
                    url="https://example-clinic.sa/",
                    title="Example Clinic",
                    text="Friendly modern dental care in Riyadh.",
                    theme_color="#0B5D3B",
                    language="ar",
                )
            ],
            content_hash="abc123",
        )
        monkeypatch.setattr(
            "app.features.brand_research.fetch_extract.fetch_site",
            AsyncMock(return_value=site),
        )

        from app.integrations.llm.fakes import FakeLLMProvider
        from app.integrations.llm.router import LLMTaskRouter

        fake = FakeLLMProvider()
        fake_router = LLMTaskRouter(
            providers={"openai": fake, "anthropic": fake},
            settings=get_settings().llm,
        )
        monkeypatch.setattr(
            "app.jobs.handlers.brand_research.get_llm_router",
            lambda settings: fake_router,
        )

        async def _drain() -> None:
            for _ in range(40):
                claimed = await claim(
                    db_session, queue="ai", batch_size=10, worker_id="test-research"
                )
                for job in claimed:
                    if job["type"] == "ai.brand_research":
                        await handle_brand_research(job["payload"])
                        return
                await asyncio.sleep(0.1)

        drain_task = asyncio.create_task(_drain())
        try:
            response = await client.post(
                "/v1/ai/brand/relearn",
                json={"brandId": brand["id"], "url": "https://example-clinic.sa/"},
                headers={"Idempotency-Key": f"relearn-{uuid.uuid4()}"},
                timeout=30.0,
            )
        finally:
            await drain_task

        assert response.status_code == 200, response.text
        assert "text/event-stream" in response.headers["content-type"]
        events = _parse_sse(response.text)
        types = [e["type"] for e in events]
        assert "step" in types
        assert "proposal" in types
        assert "done" in types
        proposal = next(e for e in events if e["type"] == "proposal")
        assert "patch" in proposal
        assert "confidence" in proposal
        assert "sources" in proposal
        assert "warnings" in proposal
        assert proposal["patch"].get("guidelines") or proposal["patch"].get("pillars")

        rows = (
            (
                await db_session.execute(
                    select(Run).where(Run.kind == "brand_relearn").order_by(Run.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        assert rows
        get_resp = await client.get(f"/v1/ai/runs/{rows[0].id}")
        assert get_resp.status_code == 200, get_resp.text
        body = get_resp.json()
        assert body["kind"] == "brand_relearn"
        assert body["status"] in {"succeeded", "partial"}
    finally:
        monkeypatch.delenv("RESEARCH__APPROACH", raising=False)
        get_settings.cache_clear()


_FAST_HTML = """\
<!DOCTYPE html>
<html lang="ar">
<head>
  <meta name="theme-color" content="#0B5D3B">
  <title>Example Clinic</title>
</head>
<body>
  <img class="logo" src="/logo.png" width="120" alt="logo">
  <p>Friendly modern dental care hospital clinic in Riyadh.</p>
</body>
</html>
"""


@pytest.mark.asyncio
async def test_relearn_fast_approach_streams_proposal(
    client: AsyncClient,
    seed_member: SeedMember,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default fast path: SSRF fetch mocked, FakeLLM structured extract."""
    monkeypatch.setenv("RESEARCH__APPROACH", "fast")
    get_settings.cache_clear()
    try:
        org_id, _user_id, raw_token = await seed_member(role="owner")
        _login(client, raw_token)
        brand = await _create_brand(client, org_id)

        async def _fake_get(
            url: str, *, timeout_seconds: float = 10.0
        ) -> dict[str, object] | None:
            if "/about" in url:
                return None
            return {"url": "https://example-clinic.sa/", "html": _FAST_HTML, "status": 200}

        monkeypatch.setattr(
            "app.features.brand_research.fast_extract._safe_get",
            _fake_get,
        )

        from app.integrations.llm.fakes import FakeLLMProvider
        from app.integrations.llm.router import LLMTaskRouter

        fake = FakeLLMProvider()
        fake_router = LLMTaskRouter(
            providers={"openai": fake, "anthropic": fake},
            settings=get_settings().llm,
        )
        monkeypatch.setattr(
            "app.jobs.handlers.brand_research.get_llm_router",
            lambda settings: fake_router,
        )

        async def _drain() -> None:
            for _ in range(40):
                claimed = await claim(
                    db_session, queue="ai", batch_size=10, worker_id="test-research-fast"
                )
                for job in claimed:
                    if job["type"] == "ai.brand_research":
                        await handle_brand_research(job["payload"])
                        return
                await asyncio.sleep(0.1)

        drain_task = asyncio.create_task(_drain())
        try:
            response = await client.post(
                "/v1/ai/brand/relearn",
                json={"brandId": brand["id"], "url": "https://example-clinic.sa/"},
                headers={"Idempotency-Key": f"relearn-fast-{uuid.uuid4()}"},
                timeout=30.0,
            )
        finally:
            await drain_task

        assert response.status_code == 200, response.text
        events = _parse_sse(response.text)
        types = [e["type"] for e in events]
        assert "proposal" in types
        assert "done" in types
        proposal = next(e for e in events if e["type"] == "proposal")
        assert proposal["patch"].get("guidelines") or proposal["patch"].get("pillars")
        assert proposal["patch"]["guidelines"].get("colors") or proposal["patch"].get("logoUrl")
    finally:
        monkeypatch.delenv("RESEARCH__APPROACH", raising=False)
        get_settings.cache_clear()
