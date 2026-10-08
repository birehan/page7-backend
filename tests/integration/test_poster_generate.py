"""Poster style enqueues Qwen and passes brand logo for post-process composite."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.visuals.schemas import GenerateVisualsBody
from app.features.visuals.service import start_generate
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Poster Brand {uuid.uuid4().hex[:6]}",
            "industry": "retail",
            "city": "Riyadh",
            "website": "https://example-shop.sa/",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_poster_enqueue_includes_logo_reference(
    client: AsyncClient,
    seed_member: SeedMember,
    db_session: AsyncSession,
) -> None:
    org_id, user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)

    logo_url = (
        f"{get_settings().storage.public_base_url.rstrip('/')}"
        f"/orgs/{org_id}/brands/{brand['id']}/logo/test/original"
    )
    await db_session.execute(
        text(
            "UPDATE brands SET logo_url = :logo, version = version + 1 "
            "WHERE id = :id"
        ),
        {"logo": logo_url, "id": brand["id"]},
    )
    await db_session.commit()

    body = GenerateVisualsBody.model_validate(
        {
            "brandId": brand["id"],
            "prompt": "a coffee shop poster for Riyadh",
            "style": "poster",
            "aspect": "square",
            "count": 2,
            "useBrandColors": True,
            "useBrandLogo": True,
            "headline": "عرض خاص",
        }
    )
    # Enqueue only — do not consume the SSE stream.
    await start_generate(
        db_session,
        organization_id=org_id,
        user_id=user_id,
        user_name="Owner",
        body=body,
        idempotency_key=f"poster-{uuid.uuid4()}",
        settings=get_settings(),
    )

    row = (
        await db_session.execute(
            text(
                "SELECT payload FROM jobs "
                "WHERE type = 'ai.visuals_generate' "
                "ORDER BY id DESC LIMIT 1"
            )
        )
    ).mappings().one()
    payload = row["payload"]
    assert payload["param_profile"] == "qwen"
    assert payload["model_id"] == "alibaba/qwen-image-3/text-to-image"
    # Logo is composited after generation (pixel-faithful), not sent as a model ref.
    assert payload["logo_url"] == logo_url
    assert payload["force_logo"] is True
    assert logo_url not in payload["reference_image_urls"]
    assert "عرض خاص" in payload["augmented_prompt"]
    assert "no alcohol" in payload["augmented_prompt"]
