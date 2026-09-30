"""Live Fal image-generation smoke — skipped unless RUN_LIVE_TESTS=1.

Budget-capped: one single-image square_hd generation. Asserts the provider returns
a real URL and that copying into gen/pending/ lands an object in public storage.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.core.config import get_settings
from app.integrations.imagegen.fal import FalImageProvider
from app.integrations.imagegen.ports import ImageGenerationRequest
from app.integrations.storage import get_object_storage
from app.integrations.storage.ports import CopyObjectRequest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="Set RUN_LIVE_TESTS=1 to hit real Fal accounts",
    ),
]


def _api_key() -> str:
    settings = get_settings()
    if settings.imagegen.api_key is not None:
        return settings.imagegen.api_key.get_secret_value()
    fal_key = os.environ.get("FAL_KEY", "").strip()
    if fal_key:
        return fal_key
    pytest.skip("IMAGEGEN__API_KEY or FAL_KEY required for live fal test")


@pytest.mark.asyncio
async def test_live_fal_single_square_hd_copied_to_pending() -> None:
    api_key = _api_key()
    os.environ.setdefault("FAL_KEY", api_key)
    settings = get_settings()
    provider = FalImageProvider(
        api_key=api_key,
        timeout_seconds=settings.imagegen.timeout_seconds,
    )

    result = await provider.generate_one(
        ImageGenerationRequest(
            prompt="A simple ceramic coffee cup on a white table, soft daylight, no text",
            style="photo",
            aspect="square",
            model_id="fal-ai/flux/dev",
            image_size="square_hd",
            enable_safety_checker=True,
            num_inference_steps=20,
            guidance_scale=3.5,
        )
    )

    assert result.image.url.startswith("https://")
    assert result.image.width > 0
    assert result.image.height > 0
    assert not result.image.flagged

    storage = get_object_storage(settings)
    pending_key = f"gen/pending/live-smoke/{uuid.uuid4()}/0"
    await storage.copy_object(
        CopyObjectRequest(
            source=result.image.url,
            dest_bucket="public",
            dest_key=pending_key,
            content_type="image/png",
        )
    )
    head = await storage.head_object("public", pending_key)
    assert head.exists is True
