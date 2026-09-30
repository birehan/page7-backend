"""Phase 5 media library integration tests — LocalStorage + real Postgres."""

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
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.media.models import UploadIntent
from app.integrations.stock import get_stock_provider
from app.integrations.stock.fakes import FakeStockProvider
from app.integrations.storage import get_object_storage
from app.integrations.storage.local import LocalStorage
from app.jobs.handlers.process_media import handle as process_media
from app.main import create_app
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


def _png_bytes(*, width: int = 640, height: int = 480) -> bytes:
    img = Image.new("RGB", (width, height), color=(20, 120, 200))
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

    # Process-media / cleanup handlers call get_object_storage(get_settings())
    # directly — patch those modules to use the same LocalStorage.
    import app.jobs.handlers.cleanup_upload_intents as cleanup_mod
    import app.jobs.handlers.process_media as process_mod

    original_process = process_mod.get_object_storage  # type: ignore[attr-defined]
    original_cleanup = cleanup_mod.get_object_storage  # type: ignore[attr-defined]
    process_mod.get_object_storage = lambda _settings: storage  # type: ignore[attr-defined, assignment]
    cleanup_mod.get_object_storage = lambda _settings: storage  # type: ignore[attr-defined, assignment]

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
        cleanup_mod.get_object_storage = original_cleanup  # type: ignore[attr-defined]
        app.dependency_overrides.clear()


async def _create_brand(client: AsyncClient, org_id: uuid.UUID) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Media Brand {uuid.uuid4().hex[:6]}",
            "industry": "retail",
            "city": "Riyadh",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def test_upload_complete_process_ready_lifecycle(
    media_client: tuple[AsyncClient, LocalStorage],
    seed_member: SeedMember,
) -> None:
    client, _storage = media_client
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = brand["id"]
    png = _png_bytes()

    slot = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/upload-url",
        json={"filename": "photo.png", "contentType": "image/png", "size": len(png)},
    )
    assert slot.status_code == 200, slot.text
    body = slot.json()
    assert "uploadUrl" in body and "assetId" in body
    assert body["headers"]["Content-Type"] == "image/png"

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
    assert item["kind"] == "image"
    assert item["width"] == 640
    assert item["height"] == 480
    assert item["id"] == body["assetId"]

    await process_media({"media_asset_id": item["id"]})

    listed = await client.get(f"/v1/orgs/{org_id}/brands/{brand_id}/media")
    assert listed.status_code == 200
    page = listed.json()
    assert "items" in page
    assert any(m["id"] == item["id"] for m in page["items"])


async def test_upload_url_rejects_oversized_and_bad_type(
    media_client: tuple[AsyncClient, LocalStorage],
    seed_member: SeedMember,
) -> None:
    client, _storage = media_client
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = brand["id"]

    too_big = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/upload-url",
        json={
            "filename": "huge.png",
            "contentType": "image/png",
            "size": 11 * 1024 * 1024,
        },
    )
    assert too_big.status_code == 413
    assert too_big.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"

    bad_type = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/upload-url",
        json={"filename": "x.pdf", "contentType": "application/pdf", "size": 100},
    )
    assert bad_type.status_code == 415
    assert bad_type.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


async def test_delete_never_returns_media_in_use(
    media_client: tuple[AsyncClient, LocalStorage],
    seed_member: SeedMember,
) -> None:
    """Phase 5 negative assertion: DELETE is an unconditional soft delete."""
    client, _storage = media_client
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = brand["id"]
    png = _png_bytes()

    slot = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/upload-url",
        json={"filename": "photo.png", "contentType": "image/png", "size": len(png)},
    )
    body = slot.json()
    await client.put(
        urlparse(body["uploadUrl"]).path,
        content=png,
        headers={"Content-Type": "image/png"},
    )
    completed = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/{body['assetId']}/complete"
    )
    media_id = completed.json()["id"]

    deleted = await client.delete(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/{media_id}"
    )
    assert deleted.status_code == 204


async def test_stock_search_and_focal_point(
    media_client: tuple[AsyncClient, LocalStorage],
    seed_member: SeedMember,
) -> None:
    client, _storage = media_client
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = brand["id"]

    stock = await client.get(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/stock",
        params={"q": "coffee", "page": 1},
    )
    assert stock.status_code == 200, stock.text
    assert len(stock.json()["items"]) > 0
    assert stock.json()["nextPage"] == 2

    png = _png_bytes()
    slot = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/upload-url",
        json={"filename": "photo.png", "contentType": "image/png", "size": len(png)},
    )
    await client.put(
        urlparse(slot.json()["uploadUrl"]).path,
        content=png,
        headers={"Content-Type": "image/png"},
    )
    completed = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/{slot.json()['assetId']}/complete"
    )
    media_id = completed.json()["id"]

    patched = await client.patch(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/{media_id}/focal-point",
        json={"focalPoint": {"x": 0.3, "y": 0.7}},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["focalPoint"] == {"x": 0.3, "y": 0.7}

    alt = await client.patch(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/{media_id}",
        json={"altAr": "صورة", "altEn": "photo"},
    )
    assert alt.status_code == 200
    assert alt.json()["altEn"] == "photo"


async def test_orphan_intent_cleanup(
    media_client: tuple[AsyncClient, LocalStorage],
    db_session: AsyncSession,
    seed_member: SeedMember,
) -> None:
    _client, storage = media_client
    org_id, user_id, _token = await seed_member(role="owner")
    from app.features.brands.models import Brand

    brand = Brand(
        organization_id=org_id,
        name="Cleanup Brand",
        industry="retail",
        city="Riyadh",
        guidelines={},
    )
    db_session.add(brand)
    await db_session.flush()

    intent_id = uuid.uuid4()
    key = f"tmp/{intent_id}/original"
    await storage.put_object("public", key, b"orphan", content_type="image/png")
    intent = UploadIntent(
        id=intent_id,
        organization_id=org_id,
        brand_id=brand.id,
        created_by=user_id,
        r2_bucket="pgblank",
        r2_key=key,
        original_filename="orphan.png",
        content_type="image/png",
        declared_size=6,
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(intent)
    await db_session.commit()

    from app.jobs.handlers.cleanup_upload_intents import handle as cleanup

    await cleanup({})
    assert (await storage.head_object("public", key)).exists is False


async def test_svg_renamed_as_png_rejected_at_complete(
    media_client: tuple[AsyncClient, LocalStorage],
    seed_member: SeedMember,
) -> None:
    client, _storage = media_client
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)
    brand = await _create_brand(client, org_id)
    brand_id = brand["id"]
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

    slot = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/upload-url",
        json={"filename": "evil.png", "contentType": "image/png", "size": len(svg)},
    )
    assert slot.status_code == 200
    await client.put(
        urlparse(slot.json()["uploadUrl"]).path,
        content=svg,
        headers={"Content-Type": "image/png"},
    )
    completed = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/{slot.json()['assetId']}/complete"
    )
    assert completed.status_code == 415
    assert completed.json()["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"
