"""Phase 12 weekly insights — one report per brand per Riyadh week."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import riyadh_today, utc_now
from app.features.analytics.models import InsightReport, MetricSnapshot
from app.features.analytics.service import previous_riyadh_week_monday, run_weekly_insights
from app.features.content_ai.models import AiDecision
from app.features.notifications.models import Notification
from app.features.social_accounts.models import SocialAccount
from app.integrations.llm import get_llm_router
from app.integrations.social import reset_fake_social_provider
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Insight Brand {uuid.uuid4().hex[:6]}",
            "industry": "retail",
            "city": "Riyadh",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def _connect_instagram(
    client: AsyncClient, org_id: uuid.UUID, brand_id: str
) -> dict[str, Any]:
    oauth = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/channels/instagram/oauth-url"
    )
    assert oauth.status_code == 200, oauth.text
    channels = (
        await client.get(f"/v1/orgs/{org_id}/brands/{brand_id}/channels")
    ).json()
    ig = next(c for c in channels if c["platform"] == "instagram")
    connected = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/channels/{ig['id']}/connect"
    )
    assert connected.status_code == 200, connected.text
    return connected.json()  # type: ignore[no-any-return]


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZERNIO_API_KEY__t1", "fake-key-t1")
    monkeypatch.setenv("ZERNIO_API_KEY__t2", "fake-key-t2")
    monkeypatch.setenv("ZERNIO_API_KEY__t3", "fake-key-t3")
    monkeypatch.setenv("LLM__PROVIDER", "fake")
    get_settings.cache_clear()
    reset_fake_social_provider()


@pytest.mark.asyncio
async def test_one_report_per_week_second_run_noop(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = uuid.UUID(brand["id"])
    await _connect_instagram(client, org_id, brand["id"])

    await db_session.rollback()
    account = (
        await db_session.execute(
            select(SocialAccount).where(
                SocialAccount.brand_id == brand_id,
                SocialAccount.platform == "instagram",
            )
        )
    ).scalar_one()

    week_of = previous_riyadh_week_monday(riyadh_today())
    mid_week = week_of + timedelta(days=2)

    post_id = uuid.uuid4()
    await db_session.execute(
        text(
            """
            INSERT INTO posts (
                id, organization_id, brand_id, platform, status,
                scheduled_at, published_at, created_by, variants
            ) VALUES (
                :id, :org, :brand, 'instagram', 'published',
                :scheduled, :published, :user, '[]'::jsonb
            )
            """
        ),
        {
            "id": str(post_id),
            "org": str(org_id),
            "brand": str(brand_id),
            "scheduled": datetime(
                week_of.year, week_of.month, week_of.day, 12, tzinfo=UTC
            ),
            "published": datetime(
                week_of.year, week_of.month, week_of.day, 12, tzinfo=UTC
            ),
            "user": str(user_id),
        },
    )
    db_session.add(
        MetricSnapshot(
            organization_id=org_id,
            brand_id=brand_id,
            social_account_id=account.id,
            platform="instagram",
            post_id=post_id,
            metric_date=mid_week,
            impressions=5000,
            reach=4200,
            likes=120,
            comments=8,
            shares=3,
            saves=5,
            captured_at=utc_now(),
        )
    )
    await db_session.commit()

    settings = get_settings()
    router = get_llm_router(settings)

    first = await run_weekly_insights(
        db_session, settings=settings, router=router, week_of=week_of
    )
    await db_session.commit()
    assert first == 1

    reports = (
        await db_session.execute(
            select(InsightReport).where(
                InsightReport.brand_id == brand_id,
                InsightReport.week_of == week_of,
            )
        )
    ).scalars().all()
    assert len(reports) == 1
    report = reports[0]
    assert report.what_happened
    assert report.metrics["reach"] == 4200
    assert isinstance(report.series, list)
    assert isinstance(report.platform_breakdown, list)
    assert report.platform_breakdown[0]["platform"] == "instagram"

    decisions = (
        await db_session.execute(
            select(AiDecision).where(
                AiDecision.brand_id == brand_id,
                AiDecision.kind == "insight",
            )
        )
    ).scalars().all()
    assert len(decisions) == 1

    notifs = (
        await db_session.execute(
            select(Notification).where(
                Notification.organization_id == org_id,
                Notification.type == "weekly_insight",
            )
        )
    ).scalars().all()
    assert len(notifs) >= 1

    second = await run_weekly_insights(
        db_session, settings=settings, router=router, week_of=week_of
    )
    await db_session.commit()
    assert second == 0

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(InsightReport)
            .where(
                InsightReport.brand_id == brand_id,
                InsightReport.week_of == week_of,
            )
        )
    ).scalar_one()
    assert count == 1

    decision_count = (
        await db_session.execute(
            select(func.count())
            .select_from(AiDecision)
            .where(
                AiDecision.brand_id == brand_id,
                AiDecision.kind == "insight",
            )
        )
    ).scalar_one()
    assert decision_count == 1
