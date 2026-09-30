"""Contract shape checks for Stage 3 posts endpoints."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jsonschema
from httpx import AsyncClient

from app.core.config import get_settings
from tests.contract.conftest import find_endpoint
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def test_posts_endpoints_match_the_frontend_contract(
    client: AsyncClient,
    seed_member: SeedMember,
    api_contract: dict[str, Any],
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    brand = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Contract Posts {uuid.uuid4().hex[:6]}",
            "industry": "retail",
            "city": "Riyadh",
        },
    )
    assert brand.status_code == 201, brand.text
    brand_id = brand.json()["id"]
    base = f"/v1/orgs/{org_id}/brands/{brand_id}/posts"

    created = await client.post(
        base,
        json={
            "platform": "instagram",
            "scheduledAt": (datetime.now(UTC) + timedelta(days=3))
            .isoformat()
            .replace("+00:00", "Z"),
            "variants": [
                {
                    "lang": "ar",
                    "caption": "احجز موعدك معنا",
                    "hashtags": ["#موعد"],
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    jsonschema.validate(
        instance=created.json(),
        schema=find_endpoint(api_contract, "posts", "POST", path_contains="/posts")[
            "response"
        ],
    )
    post_id = created.json()["id"]

    listed = await client.get(base)
    assert listed.status_code == 200
    jsonschema.validate(
        instance=listed.json(),
        schema=find_endpoint(api_contract, "posts", "GET", path_contains="/posts")[
            "response"
        ],
    )

    got = await client.get(f"{base}/{post_id}")
    assert got.status_code == 200
    jsonschema.validate(
        instance=got.json(),
        schema=find_endpoint(
            api_contract, "posts", "GET", path_contains="/posts/:postId"
        )["response"],
    )

    patched = await client.patch(
        f"{base}/{post_id}",
        json={"internalNote": "contract note", "version": 1},
    )
    assert patched.status_code == 200, patched.text
    jsonschema.validate(
        instance=patched.json(),
        schema=find_endpoint(api_contract, "posts", "PATCH")["response"],
    )

    submitted = await client.post(f"{base}/{post_id}/submit")
    assert submitted.status_code == 200, submitted.text
    jsonschema.validate(
        instance=submitted.json(),
        schema=find_endpoint(api_contract, "posts", "POST", path_contains="/submit")[
            "response"
        ],
    )

    comments = await client.post(
        f"{base}/{post_id}/comments",
        json={"body": "nudge", "lang": "en"},
    )
    assert comments.status_code == 201, comments.text
    jsonschema.validate(
        instance=comments.json(),
        schema=find_endpoint(
            api_contract, "posts", "POST", path_contains="/comments"
        )["response"],
    )

    versions = await client.get(f"{base}/{post_id}/versions")
    assert versions.status_code == 200
    jsonschema.validate(
        instance=versions.json(),
        schema=find_endpoint(
            api_contract, "posts", "GET", path_contains="/versions"
        )["response"],
    )

    queue = await client.get(f"{base}/approval-queue")
    assert queue.status_code == 200
    jsonschema.validate(
        instance=queue.json(),
        schema=find_endpoint(
            api_contract, "posts", "GET", path_contains="/approval-queue"
        )["response"],
    )

    dup = await client.post(f"{base}/{post_id}/duplicate")
    assert dup.status_code == 200, dup.text
    jsonschema.validate(
        instance=dup.json(),
        schema=find_endpoint(
            api_contract, "posts", "POST", path_contains="/duplicate"
        )["response"],
    )
