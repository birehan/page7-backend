"""Contract tests for Phase 12 analytics endpoints against api-contract.json."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from typing import Any

import jsonschema
import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.ids import new_uuid7
from app.features.analytics.models import InsightReport
from tests.contract.conftest import find_endpoint
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Analytics Brand {uuid.uuid4().hex[:6]}",
            "industry": "retail",
            "city": "Riyadh",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def _seed_report(
    db_session: AsyncSession,
    *,
    org_id: uuid.UUID,
    brand_id: uuid.UUID,
    week_of: date,
) -> InsightReport:
    report = InsightReport(
        id=new_uuid7(),
        organization_id=org_id,
        brand_id=brand_id,
        week_of=week_of,
        what_happened="Reach held steady.",
        why="Consistent posting cadence.",
        what_to_change="Post more Reels.",
        next_actions=["Schedule two Reels", "Reply faster"],
        metrics={
            "reach": 1000,
            "reachDelta": 5.0,
            "engagement": 80,
            "engagementDelta": 2.0,
            "followers": 500,
            "followersDelta": 1.0,
        },
        series=[{"date": week_of.isoformat(), "reach": 100, "engagement": 10}],
        pillar_breakdown=[],
        platform_breakdown=[
            {
                "platform": "instagram",
                "reach": 1000,
                "engagement": 80,
                "posts": 3,
            }
        ],
        generated_at=datetime.now(UTC),
    )
    db_session.add(report)
    await db_session.commit()
    return report


@pytest.mark.asyncio
async def test_analytics_endpoints_match_the_frontend_contract(
    client: AsyncClient,
    seed_member: SeedMember,
    db_session: AsyncSession,
    api_contract: dict[str, Any],
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = uuid.UUID(brand["id"])

    empty_latest = await client.get(
        f"/v1/orgs/{org_id}/brands/{brand_id}/analytics/insight-reports/latest"
    )
    assert empty_latest.status_code == 404, empty_latest.text

    await _seed_report(
        db_session,
        org_id=org_id,
        brand_id=brand_id,
        week_of=date(2026, 8, 31),
    )
    await _seed_report(
        db_session,
        org_id=org_id,
        brand_id=brand_id,
        week_of=date(2026, 8, 24),
    )

    listed = await client.get(
        f"/v1/orgs/{org_id}/brands/{brand_id}/analytics/insight-reports"
    )
    assert listed.status_code == 200, listed.text
    jsonschema.validate(
        instance=listed.json(),
        schema=find_endpoint(
            api_contract, "analytics", "GET", path_contains="/insight-reports"
        )["response"],
    )
    assert len(listed.json()["items"]) == 2
    assert listed.json()["items"][0]["weekOf"] == "2026-08-31"

    latest = await client.get(
        f"/v1/orgs/{org_id}/brands/{brand_id}/analytics/insight-reports/latest"
    )
    assert latest.status_code == 200, latest.text
    jsonschema.validate(
        instance=latest.json(),
        schema=find_endpoint(
            api_contract,
            "analytics",
            "GET",
            path_contains="/insight-reports/latest",
        )["response"],
    )
    assert latest.json()["weekOf"] == "2026-08-31"
