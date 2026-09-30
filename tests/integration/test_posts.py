"""Phase 6 Stage 3 posts workflow integration walkthrough."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient

from app.core.config import get_settings
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Posts Brand {uuid.uuid4().hex[:6]}",
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


async def test_create_edit_submit_withdraw_comments_versions_duplicate(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = brand["id"]
    base = f"/v1/orgs/{org_id}/brands/{brand_id}/posts"

    created = await client.post(base, json=_create_body())
    assert created.status_code == 201, created.text
    post = created.json()
    assert post["status"] == "draft"
    assert post["version"] == 1
    assert post["risk"]["score"] >= 0
    post_id = post["id"]

    edited = await client.patch(
        f"{base}/{post_id}",
        json={
            "variants": [
                {
                    "lang": "ar",
                    "dialect": "gulf",
                    "caption": "احجز قهوتك المحدثة معنا",
                    "hashtags": ["#قهوة", "#صباح"],
                }
            ],
            "internalNote": "tweaked caption",
            "version": 1,
        },
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["version"] == 2
    assert edited.json()["internalNote"] == "tweaked caption"

    submitted = await client.post(f"{base}/{post_id}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "in_review"
    assert submitted.json()["submittedAt"] is not None

    withdrawn = await client.post(f"{base}/{post_id}/withdraw")
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["status"] == "draft"

    # Re-submit so request-changes / reject paths have an in_review post later.
    await client.post(f"{base}/{post_id}/submit")

    comment = await client.post(
        f"{base}/{post_id}/comments",
        json={"body": "Looks good overall", "lang": "en"},
    )
    assert comment.status_code == 201, comment.text
    assert comment.json()["resolved"] is False
    comment_id = comment.json()["id"]

    listed_comments = await client.get(f"{base}/{post_id}/comments")
    assert listed_comments.status_code == 200
    assert len(listed_comments.json()) == 1

    resolved = await client.post(f"{base}/comments/{comment_id}/resolve")
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resolved"] is True

    versions = await client.get(f"{base}/{post_id}/versions")
    assert versions.status_code == 200
    assert len(versions.json()) >= 2

    dup = await client.post(f"{base}/{post_id}/duplicate")
    assert dup.status_code == 200, dup.text
    copy = dup.json()
    assert copy["id"] != post_id
    assert copy["status"] == "draft"
    assert copy["version"] == 1
    assert copy.get("groupId") is None
    assert copy.get("submittedAt") is None

    group = await client.get(f"{base}/{post_id}/group")
    assert group.status_code == 200
    assert len(group.json()) == 1

    queue = await client.get(f"{base}/approval-queue")
    assert queue.status_code == 200
    assert any(p["id"] == post_id for p in queue.json())

    changes = await client.post(
        f"{base}/{post_id}/request-changes",
        json={"reason": "Please soften the CTA"},
    )
    assert changes.status_code == 200, changes.text
    assert changes.json()["status"] == "changes_requested"
    assert changes.json()["changeRequestReason"] == "Please soften the CTA"

    resubmitted = await client.post(f"{base}/{post_id}/resubmit")
    assert resubmitted.status_code == 200, resubmitted.text
    assert resubmitted.json()["status"] == "in_review"

    rejected = await client.post(
        f"{base}/{post_id}/reject",
        json={"reason": "Off brand"},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["status"] == "draft"
    assert rejected.json()["rejectReason"] == "Off brand"

    missing = await client.get(f"{base}/{uuid.uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOT_FOUND"


async def test_edit_rejected_when_in_review(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = brand["id"]
    base = f"/v1/orgs/{org_id}/brands/{brand_id}/posts"

    created = await client.post(base, json=_create_body())
    post_id = created.json()["id"]
    await client.post(f"{base}/{post_id}/submit")

    patched = await client.patch(
        f"{base}/{post_id}",
        json={"internalNote": "nope", "version": created.json()["version"] + 1},
    )
    assert patched.status_code == 409
    assert patched.json()["error"]["code"] == "INVALID_TRANSITION"
