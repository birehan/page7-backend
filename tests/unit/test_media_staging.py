"""Unit tests for LocalStorage → Zernio media staging."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from app.features.publishing.media_staging import (
    ensure_media_urls_provider_fetchable,
    is_provider_fetchable_url,
    storage_key_from_media_url,
)
from app.integrations.social.fakes import FakeSocialProvider
from app.integrations.social.ports import PublishRequest
from app.integrations.social.zernio import ZernioClient
from app.integrations.storage.local import LocalStorage


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://media.example.com/a.jpg", True),
        ("http://localhost:8000/_local-storage/public/a.jpg", False),
        ("/_local-storage/public/a.jpg", False),
        ("https://127.0.0.1/a.jpg", False),
        ("https://10.0.0.2/a.jpg", False),
        ("ftp://media.example.com/a.jpg", False),
    ],
)
def test_is_provider_fetchable_url(url: str, expected: bool) -> None:
    assert is_provider_fetchable_url(url) is expected


def test_storage_key_from_local_and_absolute_urls() -> None:
    base = "http://localhost:8000"
    key = "orgs/o/brands/b/media/x/original"
    assert (
        storage_key_from_media_url(
            f"{base}/_local-storage/public/{key}", public_base_url=base
        )
        == key
    )
    assert (
        storage_key_from_media_url(
            f"/_local-storage/public/{key}", public_base_url=base
        )
        == key
    )


async def test_ensure_skips_fake_provider(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path, public_base_url="http://localhost:8000")
    await storage.put_object("public", "k/original", b"png", content_type="image/png")
    request = PublishRequest(
        content="hi",
        platforms=[{"platform": "facebook", "accountId": "a1"}],
        media_items=[
            {"type": "image", "url": "http://localhost:8000/_local-storage/public/k/original"}
        ],
        metadata={},
        idempotency_key="pub-1",
    )
    out = await ensure_media_urls_provider_fetchable(
        request,
        storage=storage,
        provider=FakeSocialProvider(),
        public_base_url="http://localhost:8000",
    )
    assert out.media_items[0]["url"].startswith("http://localhost:8000/")


async def test_ensure_stages_via_zernio_presign(tmp_path: Path) -> None:
    storage = LocalStorage(root=tmp_path, public_base_url="http://localhost:8000")
    await storage.put_object("public", "k/original", b"png-bytes", content_type="image/png")

    uploaded: dict[str, bytes] = {}

    async def presign(_request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "uploadUrl": "http://testserver/upload-target",
                "publicUrl": "https://media.zernio.com/temp/staged.png",
                "key": "temp/staged.png",
                "expiresIn": 3600,
            }
        )

    async def upload(request: Request) -> Response:
        uploaded["body"] = await request.body()
        return Response(status_code=200)

    app = Starlette(
        routes=[
            Route("/media/presign", presign, methods=["POST"]),
            Route("/upload-target", upload, methods=["PUT"]),
        ]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as http:
        zernio = ZernioClient(
            alias="t1",
            api_key="sk_test",
            base_url="http://testserver",
            client=http,
        )
        request = PublishRequest(
            content="hi",
            platforms=[{"platform": "facebook", "accountId": "a1"}],
            media_items=[
                {
                    "type": "image",
                    "url": "http://localhost:8000/_local-storage/public/k/original",
                }
            ],
            metadata={},
            idempotency_key="pub-1",
        )
        out = await ensure_media_urls_provider_fetchable(
            request,
            storage=storage,
            provider=zernio,
            public_base_url="http://localhost:8000",
        )

    assert out.media_items[0]["url"] == "https://media.zernio.com/temp/staged.png"
    assert uploaded["body"] == b"png-bytes"
