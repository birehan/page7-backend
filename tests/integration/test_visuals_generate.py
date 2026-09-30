"""Phase 11 visuals generate — FakeImageGenerationProvider, real Postgres."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.visuals.models import ImageGeneration, ImageGenerationOutput
from app.integrations.imagegen.fakes import FakeImageGenerationProvider
from app.integrations.storage.fakes import FakeObjectStorage
from app.jobs.handlers.visuals import handle as handle_visuals
from app.jobs.queue import claim
from app.sse.models import Run, RunEvent
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Visual Brand {uuid.uuid4().hex[:6]}",
            "industry": "retail",
            "city": "Riyadh",
            "website": "https://example-shop.sa/",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def _drain_loop(
    db_session: AsyncSession,
    *,
    fake: FakeImageGenerationProvider,
    storage: FakeObjectStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.jobs.handlers.visuals.get_image_generation_provider",
        lambda settings: fake,
    )
    monkeypatch.setattr(
        "app.jobs.handlers.visuals.get_object_storage",
        lambda settings: storage,
    )
    for _ in range(40):
        claimed = await claim(
            db_session, queue="ai", batch_size=10, worker_id="test-visuals"
        )
        for job in claimed:
            if job["type"] == "ai.visuals_generate":
                await handle_visuals(job["payload"])
                return
        await asyncio.sleep(0.1)


@pytest.mark.asyncio
async def test_generate_partial_batch(
    client: AsyncClient,
    seed_member: SeedMember,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """4 requested: one filtered, one timeout → partial, 2 images, 2 errors, done."""
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)

    fake = FakeImageGenerationProvider(
        flagged_indexes={1},
        timeout_indexes={2},
    )
    storage = FakeObjectStorage()
    drain_task = asyncio.create_task(
        _drain_loop(db_session, fake=fake, storage=storage, monkeypatch=monkeypatch)
    )
    try:
        response = await client.post(
            "/v1/visuals/generate",
            json={
                "brandId": brand["id"],
                "prompt": "a coffee shop storefront in Riyadh",
                "style": "photo",
                "aspect": "square",
                "count": 4,
                "useBrandColors": False,
            },
            headers={"Idempotency-Key": f"vis-partial-{uuid.uuid4()}"},
            timeout=30.0,
        )
    finally:
        await drain_task

    assert response.status_code == 200

    run = (
        await db_session.execute(
            select(Run)
            .where(Run.kind == "visuals_generate")
            .order_by(Run.created_at.desc())
        )
    ).scalars().first()
    assert run is not None
    assert run.status == "partial"

    events = (
        await db_session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
        )
    ).scalars().all()
    assert sum(1 for e in events if e.event["type"] == "image") == 2
    error_events = [e.event for e in events if e.event["type"] == "error"]
    assert len(error_events) == 2
    assert {e["code"] for e in error_events} == {
        "CONTENT_FILTERED",
        "PROVIDER_UNAVAILABLE",
    }
    assert any(e.event["type"] == "done" for e in events)

    gen_row = (
        await db_session.execute(
            select(ImageGeneration)
            .where(ImageGeneration.brand_id == uuid.UUID(brand["id"]))
            .order_by(ImageGeneration.created_at.desc())
        )
    ).scalars().first()
    assert gen_row is not None
    assert gen_row.status == "partial"
    assert gen_row.decision_id is not None

    outputs = (
        await db_session.execute(
            select(ImageGenerationOutput).where(
                ImageGenerationOutput.image_generation_id == gen_row.id
            )
        )
    ).scalars().all()
    assert len(outputs) == 2
    for out in outputs:
        assert out.r2_key is not None
        assert out.r2_key.startswith("gen/pending/")
        assert ("public", out.r2_key) in storage.objects


@pytest.mark.asyncio
async def test_generate_all_fail(
    client: AsyncClient,
    seed_member: SeedMember,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)

    fake = FakeImageGenerationProvider(unavailable_indexes={0, 1})
    storage = FakeObjectStorage()
    drain_task = asyncio.create_task(
        _drain_loop(db_session, fake=fake, storage=storage, monkeypatch=monkeypatch)
    )
    try:
        response = await client.post(
            "/v1/visuals/generate",
            json={
                "brandId": brand["id"],
                "prompt": "all fail batch",
                "style": "flat",
                "aspect": "portrait",
                "count": 2,
            },
            headers={"Idempotency-Key": f"vis-fail-{uuid.uuid4()}"},
            timeout=30.0,
        )
    finally:
        await drain_task

    assert response.status_code == 200

    run = (
        await db_session.execute(
            select(Run)
            .where(Run.kind == "visuals_generate")
            .order_by(Run.created_at.desc())
        )
    ).scalars().first()
    assert run is not None
    assert run.status == "failed"
    events = (
        await db_session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
        )
    ).scalars().all()
    assert not any(e.event["type"] == "image" for e in events)
    assert any(e.event["type"] == "error" for e in events)
    assert not any(e.event["type"] == "done" for e in events)


@pytest.mark.asyncio
async def test_generate_resume_replays_from_seq_one(
    client: AsyncClient,
    seed_member: SeedMember,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)

    fake = FakeImageGenerationProvider()
    storage = FakeObjectStorage()
    drain_task = asyncio.create_task(
        _drain_loop(db_session, fake=fake, storage=storage, monkeypatch=monkeypatch)
    )
    try:
        response = await client.post(
            "/v1/visuals/generate",
            json={
                "brandId": brand["id"],
                "prompt": "resume check",
                "style": "minimal",
                "aspect": "landscape",
                "count": 2,
            },
            headers={"Idempotency-Key": f"vis-resume-{uuid.uuid4()}"},
            timeout=30.0,
        )
    finally:
        await drain_task

    assert response.status_code == 200

    run = (
        await db_session.execute(
            select(Run)
            .where(Run.kind == "visuals_generate")
            .order_by(Run.created_at.desc())
        )
    ).scalars().first()
    assert run is not None
    assert run.status == "succeeded"

    events = (
        await db_session.execute(
            select(RunEvent).where(RunEvent.run_id == run.id).order_by(RunEvent.seq)
        )
    ).scalars().all()
    assert events[0].seq == 1
    image_events = [e for e in events if e.event["type"] == "image"]
    assert len(image_events) == 2
    for ev in image_events:
        assert "gen/pending" in ev.event["url"] or "media.test" in ev.event["url"]
