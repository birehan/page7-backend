"""Phase 14 API-level journey: brand create → captions → approve → schedule → published.

Automated proxy for the staging walkthrough. Uses FakeLLMProvider and FakeZernioClient
(default under APP_ENV=testing) — never real provider credentials.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.publishing.models import Publication
from app.integrations.social import reset_fake_social_provider
from app.jobs.handlers.publish_post import handle as handle_publish_post
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


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


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Journey Brand {uuid.uuid4().hex[:6]}",
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


async def _connect_instagram(
    client: AsyncClient, org_id: uuid.UUID, brand_id: str
) -> None:
    oauth = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/channels/instagram/oauth-url"
    )
    assert oauth.status_code == 200, oauth.text
    channels = (await client.get(f"/v1/orgs/{org_id}/brands/{brand_id}/channels")).json()
    ig = next(c for c in channels if c["platform"] == "instagram")
    connected = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/channels/{ig['id']}/connect"
    )
    assert connected.status_code == 200, connected.text


@pytest.fixture(autouse=True)
def _reset_fake() -> None:
    reset_fake_social_provider()


@pytest.mark.asyncio
async def test_brand_to_published_journey(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    # --- brand ---
    brand = await _create_brand(client, org_id)
    await _connect_instagram(client, org_id, brand["id"])

    # --- caption generation (FakeLLM) ---
    captions = await client.post(
        "/v1/ai/captions",
        json={
            "brandId": brand["id"],
            "platforms": ["instagram"],
            "intent": "generate",
            "dialect": "gulf",
            "count": 1,
            "pillarId": brand["pillars"][0]["id"] if brand.get("pillars") else None,
        },
        headers={"Idempotency-Key": f"captions-{uuid.uuid4()}"},
    )
    assert captions.status_code == 200, captions.text
    events = _parse_sse(captions.text)
    assert events[-1]["type"] == "done"
    decision_id = events[-1]["decisionId"]
    variant = next(e["variant"] for e in events if e["type"] == "variant_done")

    # --- create post from caption, approve, schedule (due now) ---
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    scheduled_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    created = await client.post(
        base,
        json={
            "platform": "instagram",
            "scheduledAt": scheduled_at,
            "variants": [variant],
            "pillarId": brand["pillars"][0]["id"] if brand.get("pillars") else None,
            "aiDecisionId": decision_id,
        },
    )
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]

    submitted = await client.post(f"{base}/{post_id}/submit")
    assert submitted.status_code == 200, submitted.text

    approved = await client.post(f"{base}/{post_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "scheduled"

    await db_session.rollback()
    epoch_row = (
        await db_session.execute(
            text("SELECT schedule_epoch FROM posts WHERE id = :id"),
            {"id": post_id},
        )
    ).fetchone()
    assert epoch_row is not None
    epoch = int(epoch_row.schedule_epoch)

    # --- publish via FakeZernio ---
    fake = reset_fake_social_provider()
    await handle_publish_post(
        {
            "post_id": post_id,
            "epoch": epoch,
            "organization_id": str(org_id),
            "trigger": "scheduled",
            "run_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
    )

    await db_session.rollback()
    post_row = (
        await db_session.execute(
            text("SELECT status, zernio_post_id FROM posts WHERE id = :id"),
            {"id": post_id},
        )
    ).fetchone()
    assert post_row is not None
    assert post_row.status == "published"
    assert post_row.zernio_post_id
    assert fake.publish_call_count() == 1

    pubs = (
        await db_session.execute(
            select(Publication).where(Publication.post_id == uuid.UUID(post_id))
        )
    ).scalars().all()
    assert len(pubs) == 1
    assert pubs[0].status == "published"
