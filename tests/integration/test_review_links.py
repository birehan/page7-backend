"""Phase 6 Stage 5 — review links integration tests."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from tests.db_fixtures import SeedMember


@pytest.fixture(autouse=True)
async def _isolate_review_rate_limits(db_session: AsyncSession) -> AsyncIterator[None]:
    """Guest review buckets are per-IP; ASGI tests share one client host."""
    await db_session.execute(text("TRUNCATE TABLE rate_limits"))
    await db_session.commit()
    yield
    await db_session.execute(text("TRUNCATE TABLE rate_limits"))
    await db_session.commit()


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Review Brand {uuid.uuid4().hex[:6]}",
            "industry": "retail",
            "city": "Riyadh",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def _create_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "platform": "instagram",
        "scheduledAt": (datetime.now(UTC) + timedelta(days=2))
        .isoformat()
        .replace("+00:00", "Z"),
        "variants": [
            {
                "lang": "ar",
                "dialect": "gulf",
                "caption": "احجز قهوتك الصباحية معنا اليوم",
                "hashtags": ["#قهوة"],
            }
        ],
    }
    body.update(overrides)
    return body


async def _submit_in_review(
    client: AsyncClient, base: str, **create_overrides: Any
) -> dict[str, Any]:
    created = await client.post(base, json=_create_body(**create_overrides))
    assert created.status_code == 201, created.text
    post = created.json()
    submitted = await client.post(f"{base}/{post['id']}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "in_review"
    return submitted.json()  # type: ignore[no-any-return]


async def test_create_guest_get_and_approve(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = brand["id"]
    base = f"/v1/orgs/{org_id}/brands/{brand_id}/posts"

    post = await _submit_in_review(client, base)
    post_id = post["id"]

    created = await client.post(
        f"{base}/{post_id}/review-links",
        json={"expiresInDays": 7, "locale": "ar"},
    )
    assert created.status_code == 200, created.text
    payload = created.json()
    assert "token" in payload
    assert payload["token"]
    assert "/ar/review/" in payload["url"]
    assert "expiresAt" in payload
    token = payload["token"]

    # Guest fetch — no session cookie required.
    client.cookies.clear()
    guest = await client.get(f"/v1/review/{token}")
    assert guest.status_code == 200, guest.text
    body = guest.json()
    assert body["token"] == token
    assert body["brandName"] == brand["name"]
    assert body["locale"] == "ar"
    assert body.get("decision") is None
    assert len(body["posts"]) == 1
    public_post = body["posts"][0]
    assert "internalNote" not in public_post
    assert "risk" not in public_post
    assert "id" not in public_post
    assert public_post["platform"] == "instagram"
    assert "scheduledDateKey" in public_post
    assert public_post["variants"]

    decided = await client.post(
        f"/v1/review/{token}/decision",
        json={
            "decision": "approved",
            "reviewerName": "Client Owner",
        },
        headers={"Idempotency-Key": f"review:{token}"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["decision"] == "approved"
    assert decided.json()["decidedAt"]

    after = await client.get(f"/v1/review/{token}")
    assert after.status_code == 200
    assert after.json()["decision"] == "approved"
    assert after.json()["decidedByName"] == "Client Owner"

    # Post should be scheduled (or approved if frozen — not frozen here).
    _login(client, raw_token)
    fetched = await client.get(f"{base}/{post_id}")
    assert fetched.status_code == 200
    assert fetched.json()["status"] in ("scheduled", "approved")


async def test_decide_changes_requested(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    post = await _submit_in_review(client, base)

    link = await client.post(
        f"{base}/{post['id']}/review-links",
        json={"expiresInDays": 5, "locale": "en"},
    )
    token = link.json()["token"]
    client.cookies.clear()

    missing = await client.post(
        f"/v1/review/{token}/decision",
        json={"decision": "changes_requested", "reviewerName": "Client"},
        headers={"Idempotency-Key": f"review:{token}"},
    )
    assert missing.status_code == 422, missing.text

    decided = await client.post(
        f"/v1/review/{token}/decision",
        json={
            "decision": "changes_requested",
            "reviewerName": "Client",
            "comment": "Please soften the CTA",
        },
        headers={"Idempotency-Key": f"review:{token}:changes"},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["decision"] == "changes_requested"

    _login(client, raw_token)
    fetched = await client.get(f"{base}/{post['id']}")
    assert fetched.json()["status"] == "changes_requested"
    assert fetched.json()["changeRequestReason"] == "Please soften the CTA"


async def test_frozen_snapshot_ignores_later_group_edits(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    group_id = str(uuid.uuid4())

    a = await _submit_in_review(client, base, groupId=group_id, platform="instagram")
    b = await _submit_in_review(client, base, groupId=group_id, platform="facebook")

    link = await client.post(
        f"{base}/{a['id']}/review-links",
        json={"expiresInDays": 7, "locale": "ar"},
    )
    token = link.json()["token"]

    # Enlarge the group after the link is created.
    c = await _submit_in_review(client, base, groupId=group_id, platform="tiktok")
    assert c["id"] not in {a["id"], b["id"]}

    client.cookies.clear()
    guest = await client.get(f"/v1/review/{token}")
    assert guest.status_code == 200, guest.text
    platforms = sorted(p["platform"] for p in guest.json()["posts"])
    assert platforms == ["facebook", "instagram"]
    assert len(guest.json()["posts"]) == 2


async def test_partial_applicability_skips_withdrawn_sibling(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    group_id = str(uuid.uuid4())

    a = await _submit_in_review(client, base, groupId=group_id, platform="instagram")
    b = await _submit_in_review(client, base, groupId=group_id, platform="facebook")

    link = await client.post(
        f"{base}/{a['id']}/review-links",
        json={"expiresInDays": 7, "locale": "en"},
    )
    token = link.json()["token"]

    withdrawn = await client.post(f"{base}/{a['id']}/withdraw")
    assert withdrawn.status_code == 200
    assert withdrawn.json()["status"] == "draft"

    client.cookies.clear()
    decided = await client.post(
        f"/v1/review/{token}/decision",
        json={"decision": "approved", "reviewerName": "Guest"},
        headers={"Idempotency-Key": f"review:{token}"},
    )
    assert decided.status_code == 200, decided.text

    _login(client, raw_token)
    a_after = await client.get(f"{base}/{a['id']}")
    b_after = await client.get(f"{base}/{b['id']}")
    assert a_after.json()["status"] == "draft"
    assert b_after.json()["status"] in ("scheduled", "approved")


async def test_decision_idempotency_replay(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    post = await _submit_in_review(client, base)

    link = await client.post(
        f"{base}/{post['id']}/review-links",
        json={"expiresInDays": 7, "locale": "ar"},
    )
    token = link.json()["token"]
    client.cookies.clear()

    body = {"decision": "approved", "reviewerName": "Replay Guest"}
    headers = {"Idempotency-Key": f"review:{token}"}
    first = await client.post(
        f"/v1/review/{token}/decision", json=body, headers=headers
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        f"/v1/review/{token}/decision", json=body, headers=headers
    )
    assert second.status_code == 200, second.text
    assert second.json() == first.json()


async def test_expired_link_returns_404(
    client: AsyncClient,
    seed_member: SeedMember,
    db_session: AsyncSession,
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    post = await _submit_in_review(client, base)

    link = await client.post(
        f"{base}/{post['id']}/review-links",
        json={"expiresInDays": 1, "locale": "ar"},
    )
    token = link.json()["token"]
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    await db_session.execute(
        text(
            "UPDATE review_links SET expires_at = now() - interval '1 hour' "
            "WHERE token_hash = :h"
        ),
        {"h": token_hash},
    )
    await db_session.commit()

    client.cookies.clear()
    guest = await client.get(f"/v1/review/{token}")
    assert guest.status_code == 404

    decided = await client.post(
        f"/v1/review/{token}/decision",
        json={"decision": "approved", "reviewerName": "Too Late"},
        headers={"Idempotency-Key": f"review:{token}"},
    )
    assert decided.status_code == 404
