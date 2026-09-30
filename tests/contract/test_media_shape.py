from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import jsonschema
import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image

from app.core.config import get_settings
from app.integrations.stock import get_stock_provider
from app.integrations.stock.fakes import FakeStockProvider
from app.integrations.storage import get_object_storage
from app.integrations.storage.local import LocalStorage
from app.main import create_app
from tests.contract.conftest import find_endpoint
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


def _png_bytes() -> bytes:
    img = Image.new("RGB", (400, 300), color=(10, 20, 30))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
async def media_client(tmp_path: Path) -> AsyncIterator[AsyncClient]:
    app = create_app()
    storage = LocalStorage(
        root=tmp_path, public_base_url="https://test", public_bucket="pgblank"
    )
    app.dependency_overrides[get_object_storage] = lambda: storage
    app.dependency_overrides[get_stock_provider] = lambda: FakeStockProvider()
    allowed_origin = get_settings().cors.allowed_origins[0]
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="https://test",
        headers={"Origin": allowed_origin},
    ) as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_media_endpoints_match_the_frontend_contract(
    media_client: AsyncClient,
    seed_member: SeedMember,
    api_contract: dict[str, Any],
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(media_client, raw_token)

    created = await media_client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Contract Media {uuid.uuid4().hex[:6]}",
            "industry": "retail",
            "city": "Riyadh",
        },
    )
    assert created.status_code == 201, created.text
    brand_id = created.json()["id"]
    png = _png_bytes()

    slot = await media_client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/upload-url",
        json={"filename": "photo.png", "contentType": "image/png", "size": len(png)},
    )
    assert slot.status_code == 200, slot.text
    jsonschema.validate(
        instance=slot.json(),
        schema=find_endpoint(
            api_contract, "media", "POST", path_contains="/upload-url"
        )["response"],
    )

    await media_client.put(
        urlparse(slot.json()["uploadUrl"]).path,
        content=png,
        headers={"Content-Type": "image/png"},
    )
    completed = await media_client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/{slot.json()['assetId']}/complete"
    )
    assert completed.status_code == 200, completed.text
    jsonschema.validate(
        instance=completed.json(),
        schema=find_endpoint(
            api_contract, "media", "POST", path_contains="/complete"
        )["response"],
    )

    listed = await media_client.get(f"/v1/orgs/{org_id}/brands/{brand_id}/media")
    assert listed.status_code == 200
    jsonschema.validate(
        instance=listed.json(),
        schema=find_endpoint(api_contract, "media", "GET", path_contains="/media")[
            "response"
        ],
    )

    media_id = completed.json()["id"]
    patched = await media_client.patch(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/{media_id}",
        json={"altAr": "صورة", "altEn": "photo"},
    )
    assert patched.status_code == 200
    jsonschema.validate(
        instance=patched.json(),
        schema=find_endpoint(api_contract, "media", "PATCH", path_contains="/media/")[
            "response"
        ],
    )

    focal = await media_client.patch(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/{media_id}/focal-point",
        json={"focalPoint": {"x": 0.4, "y": 0.6}},
    )
    assert focal.status_code == 200
    jsonschema.validate(
        instance=focal.json(),
        schema=find_endpoint(
            api_contract, "media", "PATCH", path_contains="/focal-point"
        )["response"],
    )

    stock = await media_client.get(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/stock",
        params={"q": "coffee", "page": 1},
    )
    assert stock.status_code == 200
    jsonschema.validate(
        instance=stock.json(),
        schema=find_endpoint(api_contract, "media", "GET", path_contains="/stock")[
            "response"
        ],
    )

    deleted = await media_client.delete(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/{media_id}"
    )
    assert deleted.status_code == 204
