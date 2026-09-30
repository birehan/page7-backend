"""Credential-gated live tests against real R2 / Unsplash.

Skipped unless RUN_LIVE_TESTS=1 and the relevant STORAGE__* / STOCK__* env
vars are set. Uses a dedicated `tests/` key prefix and cleans up after itself.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from app.integrations.storage.ports import PresignUploadRequest
from app.integrations.storage.r2 import R2Storage

pytestmark = pytest.mark.live


def _live_enabled() -> bool:
    return os.environ.get("RUN_LIVE_TESTS") == "1"


def _r2_ready() -> bool:
    return bool(
        os.environ.get("STORAGE__R2_ACCOUNT_ID")
        and os.environ.get("STORAGE__R2_ACCESS_KEY_ID")
        and os.environ.get("STORAGE__R2_SECRET_ACCESS_KEY")
        and os.environ.get("STORAGE__PUBLIC_BUCKET")
        and os.environ.get("STORAGE__PUBLIC_BASE_URL")
    )


@pytest.mark.skipif(not _live_enabled() or not _r2_ready(), reason="live R2 not configured")
async def test_live_r2_presign_put_head_round_trip() -> None:
    storage = R2Storage(
        account_id=os.environ["STORAGE__R2_ACCOUNT_ID"],
        access_key_id=os.environ["STORAGE__R2_ACCESS_KEY_ID"],
        secret_access_key=os.environ["STORAGE__R2_SECRET_ACCESS_KEY"],
        public_bucket=os.environ.get("STORAGE__PUBLIC_BUCKET", "pgblank"),
        public_base_url=os.environ["STORAGE__PUBLIC_BASE_URL"],
        private_bucket=os.environ.get("STORAGE__PRIVATE_BUCKET") or None,
    )
    key = f"tests/live/{uuid.uuid4().hex}/original"
    body = b"pgblank-live-test"
    try:
        presign = await storage.presign_upload(
            PresignUploadRequest(
                bucket="public", key=key, content_type="application/octet-stream"
            )
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            put = await client.put(
                presign.upload_url,
                content=body,
                headers=presign.required_headers,
            )
        assert put.status_code in {200, 204}, put.text
        head = await storage.head_object("public", key)
        assert head.exists is True
        assert head.size_bytes == len(body)
    finally:
        await storage.delete_object("public", key)


@pytest.mark.skipif(
    not _live_enabled() or not os.environ.get("STOCK__UNSPLASH_ACCESS_KEY"),
    reason="live Unsplash not configured",
)
async def test_live_unsplash_search() -> None:
    from app.integrations.stock.unsplash import UnsplashStockProvider

    provider = UnsplashStockProvider(
        access_key=os.environ["STOCK__UNSPLASH_ACCESS_KEY"]
    )
    result = await provider.search(query="coffee", page=1)
    assert len(result.items) > 0
    assert result.items[0].attribution.provider == "unsplash"
