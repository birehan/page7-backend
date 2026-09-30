"""Phase 7 content AI integration tests — real Postgres, FakeLLMProvider."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.ids import new_uuid7
from app.features.content_ai.models import AiDecision, AiFeedback, AiUsageCounter
from app.features.media.models import MediaAsset
from app.integrations.storage import get_object_storage
from app.jobs.handlers.plan_commit import handle as handle_plan_commit
from app.jobs.queue import claim
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"AI Brand {uuid.uuid4().hex[:6]}",
            "industry": "retail",
            "city": "Riyadh",
        },
    )
    assert response.status_code == 201, response.text
    brand = response.json()
    if not brand.get("pillars"):
        generated = await client.post(
            f"/v1/orgs/{org_id}/brands/{brand['id']}/generate",
            json={
                "name": brand["name"],
                "industry": brand["industry"],
                "city": brand["city"],
            },
            headers={"Idempotency-Key": f"gen-{uuid.uuid4()}"},
        )
        assert generated.status_code == 200, generated.text
        brand = generated.json()
    return brand  # type: ignore[no-any-return]


def _parse_sse(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for block in raw.split("\n\n"):
        data_lines = [
            line.split(":", 1)[1].strip()
            for line in block.split("\n")
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        if payload == "[DONE]":
            continue
        events.append(json.loads(payload))
    return events


def _period_month() -> date:
    now = datetime.now(UTC)
    return date(now.year, now.month, 1)


async def test_captions_stream_order_and_usage_counter(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)

    response = await client.post(
        "/v1/ai/captions",
        json={
            "brandId": brand["id"],
            "platforms": ["instagram"],
            "intent": "generate",
            "dialect": "gulf",
            "count": 2,
            "pillarId": brand["pillars"][0]["id"] if brand.get("pillars") else None,
        },
    )
    assert response.status_code == 200, response.text
    events = _parse_sse(response.text)
    types = [e["type"] for e in events]
    assert types[0] == "step"
    assert "variant_done" in types
    assert "risk" in types
    assert "hashtags" in types
    assert types[-1] == "done"
    decision_id = uuid.UUID(events[-1]["decisionId"])

    decision = (
        await db_session.execute(sa.select(AiDecision).where(AiDecision.id == decision_id))
    ).scalar_one()
    assert decision.kind == "captions"
    assert decision.status in {"succeeded", "partial"}
    assert decision.organization_id == org_id

    counter = (
        await db_session.execute(
            sa.select(AiUsageCounter).where(
                AiUsageCounter.organization_id == org_id,
                AiUsageCounter.period_month == _period_month(),
            )
        )
    ).scalar_one()
    assert counter.generations >= 1
    assert counter.prompt_tokens >= 0
    assert counter.cost_usd >= Decimal("0")


async def test_feedback_upsert_toggles(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    captions = await client.post(
        "/v1/ai/captions",
        json={
            "brandId": brand["id"],
            "platforms": ["instagram"],
            "intent": "generate",
            "dialect": "gulf",
            "count": 1,
        },
    )
    decision_id = _parse_sse(captions.text)[-1]["decisionId"]

    up = await client.post(
        "/v1/ai/feedback",
        json={"decisionId": decision_id, "variantIndex": 0, "rating": "up"},
    )
    assert up.status_code == 204
    down = await client.post(
        "/v1/ai/feedback",
        json={"decisionId": decision_id, "variantIndex": 0, "rating": "down"},
    )
    assert down.status_code == 204

    rows = (
        await db_session.execute(
            sa.select(AiFeedback).where(
                AiFeedback.decision_id == uuid.UUID(decision_id),
                AiFeedback.user_id == user_id,
            )
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].rating == "down"


async def test_plan_preview_and_commit_idempotent(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    assert brand.get("pillars"), "brand needs pillars for plan commit"

    start = datetime.now(UTC).date()
    end = start + timedelta(days=7)
    preview = await client.post(
        "/v1/ai/plan",
        json={
            "brandId": brand["id"],
            "from": start.isoformat(),
            "to": end.isoformat(),
            "cadence": {"instagram": 2, "facebook": 1},
            "onlyGaps": False,
        },
    )
    assert preview.status_code == 200, preview.text
    events = _parse_sse(preview.text)
    assert any(e["type"] == "plan_item" for e in events)
    done = next(e for e in events if e["type"] == "done")
    items = [e["item"] for e in events if e["type"] == "plan_item"]
    assert items

    idem = f"plan-commit:{done['decisionId']}"

    async def _drain_ai_jobs() -> None:
        # The commit SSE stream blocks until the run finishes; the worker is a
        # separate process in production, so drain concurrently here.
        for _ in range(40):
            claimed = await claim(
                db_session, queue="ai", batch_size=10, worker_id="test-worker"
            )
            for job in claimed:
                if job["type"] == "ai.plan_commit":
                    await handle_plan_commit(job["payload"])
                    return
            await asyncio.sleep(0.1)

    drain_task = asyncio.create_task(_drain_ai_jobs())
    try:
        commit1 = await client.post(
            "/v1/ai/plan/commit",
            json={
                "brandId": brand["id"],
                "decisionId": done["decisionId"],
                "items": items,
            },
            headers={"Idempotency-Key": idem},
            timeout=30.0,
        )
    finally:
        await drain_task

    assert commit1.status_code == 200, commit1.text
    commit_events = _parse_sse(commit1.text)
    assert any(e["type"] == "post_created" for e in commit_events)
    assert commit_events[-1]["type"] == "done"

    commit2 = await client.post(
        "/v1/ai/plan/commit",
        json={
            "brandId": brand["id"],
            "decisionId": done["decisionId"],
            "items": items,
        },
        headers={"Idempotency-Key": idem},
        timeout=30.0,
    )
    assert commit2.status_code == 200, commit2.text

    posts = await client.get(f"/v1/orgs/{org_id}/brands/{brand['id']}/posts")
    assert posts.status_code == 200, posts.text
    assert len(posts.json()) == len(items)


async def test_alt_text_persists_when_url_maps_to_asset(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = uuid.UUID(brand["id"])

    r2_key = f"orgs/{org_id}/brands/{brand_id}/media/{new_uuid7()}/original.jpg"
    asset = MediaAsset(
        organization_id=org_id,
        brand_id=brand_id,
        kind="image",
        source="upload",
        status="ready",
        r2_bucket=get_settings().storage.public_bucket,
        r2_key=r2_key,
        content_type="image/jpeg",
        size_bytes=1024,
        width=100,
        height=100,
        uploaded_by=user_id,
    )
    db_session.add(asset)
    await db_session.commit()

    storage = get_object_storage(get_settings())
    public_url = storage.public_url(r2_key)
    response = await client.post(
        "/v1/ai/alt-text",
        json={"brandId": brand["id"], "mediaUrl": public_url, "caption": "coffee"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["altAr"]
    assert body["altEn"]

    await db_session.refresh(asset)
    assert asset.alt_ar == body["altAr"]
    assert asset.alt_en == body["altEn"]
    assert asset.ai_decision_id is not None


async def test_alt_text_skips_persist_for_unknown_url(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    response = await client.post(
        "/v1/ai/alt-text",
        json={
            "brandId": brand["id"],
            "mediaUrl": "https://fal.media/files/generated-temp.png",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["altAr"]
    assert response.json()["altEn"]


async def test_strategy_emits_proposal_without_applying(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    before = await client.get(f"/v1/orgs/{org_id}/brands/{brand['id']}/strategy")
    assert before.status_code == 200, before.text

    response = await client.post("/v1/ai/strategy", json={"brandId": brand["id"]})
    assert response.status_code == 200, response.text
    events = _parse_sse(response.text)
    assert any(e["type"] == "proposal" for e in events)
    assert events[-1]["type"] == "done"

    after = await client.get(f"/v1/orgs/{org_id}/brands/{brand['id']}/strategy")
    assert after.status_code == 200
    assert after.json()["goals"] == before.json()["goals"]
