"""Rendered templates are publishable (kind=image, source=template)."""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from app.core.config import get_settings
from app.integrations.stock import get_stock_provider
from app.integrations.stock.fakes import FakeStockProvider
from app.integrations.storage import get_object_storage
from app.integrations.storage.local import LocalStorage
from app.jobs.handlers.process_media import handle as process_media
from app.main import create_app
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


def _png_bytes(*, width: int = 1080, height: int = 1080) -> bytes:
    img = Image.new("RGB", (width, height), color=(30, 100, 180))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
async def media_client(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncClient, LocalStorage]]:
    app = create_app()
    storage = LocalStorage(
        root=tmp_path,
        public_base_url="https://test",
        public_bucket="pgblank",
    )
    stock = FakeStockProvider()
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_stock_provider] = lambda: stock

    import app.features.visuals.keep_render as keep_mod
    import app.jobs.handlers.process_media as process_mod

    original_process = process_mod.get_object_storage  # type: ignore[attr-defined]
    original_keep = keep_mod.get_object_storage  # type: ignore[attr-defined]
    process_mod.get_object_storage = lambda _settings: storage  # type: ignore[attr-defined, assignment]
    keep_mod.get_object_storage = lambda _settings: storage  # type: ignore[attr-defined, assignment]

    allowed_origin = get_settings().cors.allowed_origins[0]
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(
            transport=transport,
            base_url="https://test",
            headers={"Origin": allowed_origin},
        ) as ac:
            yield ac, storage
    finally:
        process_mod.get_object_storage = original_process  # type: ignore[attr-defined]
        keep_mod.get_object_storage = original_keep  # type: ignore[attr-defined]
        app.dependency_overrides.clear()


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Tpl Brand {uuid.uuid4().hex[:6]}",
            "industry": "retail",
            "city": "Riyadh",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def _upload_ready_asset(
    client: AsyncClient, *, org_id: uuid.UUID, brand_id: str
) -> dict[str, Any]:
    png = _png_bytes()
    slot = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/upload-url",
        json={"filename": "tpl.png", "contentType": "image/png", "size": len(png)},
    )
    assert slot.status_code == 200, slot.text
    body = slot.json()
    put = await client.put(
        urlparse(body["uploadUrl"]).path,
        content=png,
        headers={"Content-Type": "image/png"},
    )
    assert put.status_code == 200, put.text
    completed = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/{body['assetId']}/complete"
    )
    assert completed.status_code == 200, completed.text
    item = completed.json()
    await process_media({"media_asset_id": item["id"]})
    return item  # type: ignore[no-any-return]


@pytest.mark.asyncio
async def test_render_template_is_publishable_image_with_source_filter(
    media_client: tuple[AsyncClient, LocalStorage],
    seed_member: SeedMember,
) -> None:
    client, _storage = media_client
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = brand["id"]

    asset = await _upload_ready_asset(client, org_id=org_id, brand_id=brand_id)

    rendered = await client.post(
        "/v1/visuals/render",
        json={
            "brandId": brand_id,
            "templateId": "offer-banner",
            "headline": "عرض خاص",
            "subline": "هذا الأسبوع",
            "aspect": "square",
            "assetId": asset["id"],
        },
    )
    assert rendered.status_code == 200, rendered.text
    item = rendered.json()
    assert item["kind"] == "image"
    assert item["source"] == "template"
    assert item["template"]["templateId"] == "offer-banner"

    by_source = await client.get(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media",
        params={"source": "template"},
    )
    assert by_source.status_code == 200, by_source.text
    assert any(m["id"] == item["id"] for m in by_source.json()["items"])

    by_kind = await client.get(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media",
        params={"kind": "template"},
    )
    assert by_kind.status_code == 200
    assert not any(m["id"] == item["id"] for m in by_kind.json()["items"])

    posts_base = f"/v1/orgs/{org_id}/brands/{brand_id}/posts"
    created = await client.post(
        posts_base,
        json={
            "platform": "instagram",
            "scheduledAt": (datetime.now(UTC) + timedelta(days=2))
            .isoformat()
            .replace("+00:00", "Z"),
            "variants": [
                {
                    "lang": "ar",
                    "dialect": "gulf",
                    "caption": "عرض خاص هذا الأسبوع في الرياض",
                    "hashtags": ["#عرض"],
                }
            ],
            "media": [
                {
                    "id": item["id"],
                    "kind": "image",
                    "url": item["url"],
                    "alt": "عرض",
                    "libraryId": item["id"],
                }
            ],
        },
    )
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]

    submitted = await client.post(f"{posts_base}/{post_id}/submit")
    assert submitted.status_code == 200, submitted.text
    approved = await client.post(f"{posts_base}/{post_id}/approve")
    assert approved.status_code == 200, approved.text
    assert approved.json()["status"] == "scheduled"
