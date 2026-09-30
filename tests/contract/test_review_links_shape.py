"""Contract shape checks for Stage 5 review-links endpoints."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import jsonschema
import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from tests.contract.conftest import find_endpoint
from tests.db_fixtures import SeedMember


@pytest.fixture(autouse=True)
async def _isolate_review_rate_limits(db_session: AsyncSession) -> AsyncIterator[None]:
    await db_session.execute(text("TRUNCATE TABLE rate_limits"))
    await db_session.commit()
    yield
    await db_session.execute(text("TRUNCATE TABLE rate_limits"))
    await db_session.commit()


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def test_review_links_match_the_frontend_contract(
    client: AsyncClient,
    seed_member: SeedMember,
    api_contract: dict[str, Any],
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    brand = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Contract Review {uuid.uuid4().hex[:6]}",
            "industry": "retail",
            "city": "Riyadh",
        },
    )
    assert brand.status_code == 201, brand.text
    brand_id = brand.json()["id"]
    base = f"/v1/orgs/{org_id}/brands/{brand_id}/posts"

    created_post = await client.post(
        base,
        json={
            "platform": "instagram",
            "scheduledAt": (datetime.now(UTC) + timedelta(days=3))
            .isoformat()
            .replace("+00:00", "Z"),
            "variants": [
                {
                    "lang": "ar",
                    "dialect": "gulf",
                    "caption": "احجز موعدك معنا",
                    "hashtags": ["#موعد"],
                }
            ],
        },
    )
    assert created_post.status_code == 201, created_post.text
    post_id = created_post.json()["id"]
    submitted = await client.post(f"{base}/{post_id}/submit")
    assert submitted.status_code == 200, submitted.text

    created_link = await client.post(
        f"{base}/{post_id}/review-links",
        json={"expiresInDays": 7, "locale": "ar"},
    )
    assert created_link.status_code == 200, created_link.text
    jsonschema.validate(
        instance=created_link.json(),
        schema=find_endpoint(
            api_contract, "reviewLinks", "POST", path_contains="/review-links"
        )["response"],
    )
    token = created_link.json()["token"]

    client.cookies.clear()
    guest = await client.get(f"/v1/review/{token}")
    assert guest.status_code == 200, guest.text
    jsonschema.validate(
        instance=guest.json(),
        schema=find_endpoint(
            api_contract, "reviewLinks", "GET", path_contains="/review/:token"
        )["response"],
    )

    decided = await client.post(
        f"/v1/review/{token}/decision",
        json={"decision": "approved", "reviewerName": "Contract Guest"},
        headers={"Idempotency-Key": f"review:{token}"},
    )
    assert decided.status_code == 200, decided.text
    jsonschema.validate(
        instance=decided.json(),
        schema=find_endpoint(
            api_contract,
            "reviewLinks",
            "POST",
            path_contains="/review/:token/decision",
        )["response"],
    )
