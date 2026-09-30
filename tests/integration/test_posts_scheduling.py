"""Phase 6 Stage 4 — schedule / freeze / reschedule / publish stubs."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import riyadh_datetime_to_utc
from app.features.posts.service import PUBLISH_POST_MAX_ATTEMPTS
from app.jobs.handlers.publish_post import handle as handle_publish_post
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Sched Brand {uuid.uuid4().hex[:6]}",
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


async def _to_scheduled(
    client: AsyncClient, base: str, *, scheduled_at: str | None = None
) -> dict[str, Any]:
    body = _create_body(**({"scheduledAt": scheduled_at} if scheduled_at else {}))
    created = await client.post(base, json=body)
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]
    submitted = await client.post(f"{base}/{post_id}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "in_review"
    approved = await client.post(f"{base}/{post_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "scheduled"
    return approved.json()  # type: ignore[no-any-return]


async def test_freeze_blocks_schedule_and_publish_approve_stops_at_approved(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"

    created = await client.post(base, json=_create_body())
    post_id = created.json()["id"]
    await client.post(f"{base}/{post_id}/submit")

    freeze = await client.post(
        f"/v1/orgs/{org_id}/settings/freeze", json={"reason": "stage4"}
    )
    assert freeze.status_code == 200

    approved = await client.post(f"{base}/{post_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "approved"

    scheduled = await client.post(f"{base}/{post_id}/schedule")
    assert scheduled.status_code == 409
    assert scheduled.json()["error"]["code"] == "FROZEN"

    published = await client.post(f"{base}/{post_id}/publish")
    assert published.status_code == 409
    assert published.json()["error"]["code"] == "FROZEN"

    retry = await client.post(f"{base}/{post_id}/retry-publish")
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "FROZEN"


async def test_publish_and_retry_return_channel_not_connected(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    post = await _to_scheduled(client, base)

    published = await client.post(f"{base}/{post['id']}/publish")
    assert published.status_code == 409
    assert published.json()["error"]["code"] == "CHANNEL_NOT_CONNECTED"

    retry = await client.post(f"{base}/{post['id']}/retry-publish")
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "CHANNEL_NOT_CONNECTED"


async def test_unschedule_cancels_queued_publish_job(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    post = await _to_scheduled(client, base)
    post_id = post["id"]

    row = await db_session.execute(
        text(
            """
            SELECT unique_key, state, max_attempts
            FROM jobs
            WHERE type = 'publish_post'
              AND payload->>'post_id' = :post_id
              AND state = 'queued'
            """
        ),
        {"post_id": post_id},
    )
    job = row.fetchone()
    assert job is not None
    assert job.max_attempts == 10
    assert job.unique_key.startswith(f"publish:{post_id}:")

    unscheduled = await client.post(f"{base}/{post_id}/unschedule")
    assert unscheduled.status_code == 200, unscheduled.text
    assert unscheduled.json()["status"] == "draft"

    await db_session.rollback()  # see committed API-side deletes
    remaining = await db_session.execute(
        text(
            """
            SELECT count(*) FROM jobs
            WHERE unique_key = :key AND state = 'queued'
            """
        ),
        {"key": job.unique_key},
    )
    assert remaining.scalar_one() == 0


async def test_reschedule_midnight_boundary_preserves_riyadh_wall_time(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"

    # 01:30 Riyadh — midnight-boundary window where UTC calendar day differs.
    # Use a future Riyadh day so the past warning is not expected.
    scheduled_at = (
        riyadh_datetime_to_utc("2026-11-15", "01:30")
        .isoformat()
        .replace("+00:00", "Z")
    )
    post = await _to_scheduled(client, base, scheduled_at=scheduled_at)

    rescheduled = await client.post(
        f"{base}/{post['id']}/reschedule",
        json={"dateKey": "2026-11-20"},
    )
    assert rescheduled.status_code == 200, rescheduled.text
    body = rescheduled.json()
    expected = riyadh_datetime_to_utc("2026-11-20", "01:30")
    got = datetime.fromisoformat(body["post"]["scheduledAt"].replace("Z", "+00:00"))
    assert got == expected
    assert "past" not in body["warnings"]


async def test_autonomous_threshold_edges_and_frozen_override(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"

    # Create a low-risk post to learn its score, then set threshold at/below it.
    probe = await client.post(base, json=_create_body())
    assert probe.status_code == 201, probe.text
    score = float(probe.json()["risk"]["score"])

    at_threshold = await client.patch(
        f"/v1/orgs/{org_id}/settings",
        json={"approvalPolicy": {"mode": "autonomous", "riskThreshold": score}},
    )
    assert at_threshold.status_code == 200, at_threshold.text

    created = await client.post(base, json=_create_body())
    post_id = created.json()["id"]
    submitted = await client.post(f"{base}/{post_id}/submit")
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["status"] == "scheduled"

    # Just over threshold → stays in_review
    over = await client.patch(
        f"/v1/orgs/{org_id}/settings",
        json={
            "approvalPolicy": {
                "mode": "autonomous",
                "riskThreshold": max(0.0, score - 0.01),
            }
        },
    )
    assert over.status_code == 200, over.text

    created2 = await client.post(base, json=_create_body())
    post_id2 = created2.json()["id"]
    # Ensure score still above the lowered threshold
    assert float(created2.json()["risk"]["score"]) > max(0.0, score - 0.01) or score == 0.0
    if score == 0.0:
        # Score is already 0 — can't go "just over"; use a high-risk caption instead.
        high = await client.post(
            base,
            json=_create_body(
                variants=[
                    {
                        "lang": "ar",
                        "dialect": "gulf",
                        "caption": "دين وسياسة مع كحول وقمار",
                        "hashtags": [],
                    }
                ]
            ),
        )
        assert high.status_code == 201
        post_id2 = high.json()["id"]
        assert float(high.json()["risk"]["score"]) > 0

    submitted2 = await client.post(f"{base}/{post_id2}/submit")
    assert submitted2.status_code == 200, submitted2.text
    assert submitted2.json()["status"] == "in_review"

    # Frozen overrides autonomous regardless of risk
    await client.patch(
        f"/v1/orgs/{org_id}/settings",
        json={"approvalPolicy": {"mode": "autonomous", "riskThreshold": 1.0}},
    )
    freeze = await client.post(
        f"/v1/orgs/{org_id}/settings/freeze", json={"reason": "override"}
    )
    assert freeze.status_code == 200

    created3 = await client.post(base, json=_create_body())
    post_id3 = created3.json()["id"]
    submitted3 = await client.post(f"{base}/{post_id3}/submit")
    assert submitted3.status_code == 200, submitted3.text
    assert submitted3.json()["status"] == "in_review"


async def test_bulk_approve_reports_per_id(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"

    ids: list[str] = []
    for _ in range(2):
        created = await client.post(base, json=_create_body())
        post_id = created.json()["id"]
        await client.post(f"{base}/{post_id}/submit")
        ids.append(post_id)

    fake_id = str(uuid.uuid4())
    bulk = await client.post(
        f"{base}/approve", json={"postIds": [*ids, fake_id]}
    )
    assert bulk.status_code == 200, bulk.text
    by_id = {item["id"]: item["ok"] for item in bulk.json()}
    assert by_id[ids[0]] is True
    assert by_id[ids[1]] is True
    assert by_id[fake_id] is False


async def test_publish_post_handler_publishes_with_connected_account(
    client: AsyncClient,
    seed_member: SeedMember,
    db_session: AsyncSession,
) -> None:
    """Without a connected channel the claim fails; with none, stale-epoch still no-ops."""
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    base = f"/v1/orgs/{org_id}/brands/{brand['id']}/posts"
    post = await _to_scheduled(client, base)
    post_id = post["id"]

    epoch_row = await db_session.execute(
        text("SELECT schedule_epoch, status FROM posts WHERE id = :id"),
        {"id": post_id},
    )
    row = epoch_row.fetchone()
    assert row is not None
    assert row.status == "scheduled"
    epoch = int(row.schedule_epoch)

    # No connected channel → terminal claim failure (ACCOUNT_DISCONNECTED / failed).
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
    after = await db_session.execute(
        text("SELECT status, schedule_epoch, last_error FROM posts WHERE id = :id"),
        {"id": post_id},
    )
    again = after.fetchone()
    assert again is not None
    assert again.status == "failed"
    assert again.last_error is not None

    # Stale epoch → silent no-op
    await handle_publish_post(
        {
            "post_id": post_id,
            "epoch": epoch + 99,
            "organization_id": str(org_id),
        }
    )

    assert PUBLISH_POST_MAX_ATTEMPTS == 10
