"""Image generation provider factory (architecture/06 §2, architecture/09)."""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends

from app.core.config import Environment, Settings, get_settings
from app.integrations.imagegen.fakes import FakeImageGenerationProvider
from app.integrations.imagegen.ports import ImageGenerationProvider

_LOCAL_ENVS = (Environment.DEVELOPMENT, Environment.TESTING)


def get_image_generation_provider(
    settings: Annotated[Settings, Depends(get_settings)],
) -> ImageGenerationProvider:
    """Resolve the imagegen adapter from IMAGEGEN__PROVIDER / API key presence."""
    use_fake = settings.imagegen.provider == "fake" or (
        settings.imagegen.provider == "auto"
        and (
            settings.app_env in _LOCAL_ENVS
            or settings.imagegen.api_key is None
        )
    )
    if use_fake:
        return FakeImageGenerationProvider()

    # Lazy import so fake/auto mode never requires fal_client at process start
    # (workers that only drain non-visuals jobs, or images not yet rebuilt).
    from app.integrations.imagegen.fal import FalImageProvider

    key = settings.imagegen.api_key
    if key is None:
        raise RuntimeError("IMAGEGEN__API_KEY must be configured")
    # fal_client reads FAL_KEY from the environment; set it for the process so
    # subscribe_async authenticates without a separate client constructor.
    os.environ.setdefault("FAL_KEY", key.get_secret_value())
    return FalImageProvider(
        api_key=key.get_secret_value(),
        timeout_seconds=settings.imagegen.timeout_seconds,
    )
