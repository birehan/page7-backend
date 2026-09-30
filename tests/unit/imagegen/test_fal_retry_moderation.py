"""Unit tests for retry/backoff and Qwen NSFW moderation in FalImageProvider."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fal_client import FalClientHTTPError

from app.integrations.errors import (
    ProviderContentFilteredError,
    ProviderUnavailableError,
)
from app.integrations.imagegen.fal import FalImageProvider
from app.integrations.imagegen.ports import ImageGenerationRequest


def _req(**overrides: object) -> ImageGenerationRequest:
    base: dict[str, object] = {
        "prompt": "a coffee cup",
        "style": "photo",
        "aspect": "square",
        "model_id": "fal-ai/flux-2/flash",
        "image_size": "square_hd",
        "param_profile": "flux",
        "enable_safety_checker": True,
    }
    base.update(overrides)
    return ImageGenerationRequest.model_validate(base)


def _http_error(status_code: int, message: str) -> FalClientHTTPError:
    response = MagicMock(spec=httpx.Response)
    return FalClientHTTPError(
        message, status_code=status_code, response_headers={}, response=response
    )


def _flux_result(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "images": [{"url": "https://fal.media/x.png", "width": 1024, "height": 1024}],
        "seed": 7,
        "has_nsfw_concepts": [False],
        "request_id": "req-1",
    }
    result.update(overrides)
    return result


@pytest.mark.asyncio
async def test_retries_on_rate_limit_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FalImageProvider(api_key="test")
    monkeypatch.setattr(provider, "_sleep_before_retry", AsyncMock())

    import fal_client

    calls = {"n": 0}

    async def fake_subscribe(*args: object, **kwargs: object) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] < 2:
            raise _http_error(429, "rate limited")
        return _flux_result()

    monkeypatch.setattr(fal_client, "subscribe_async", fake_subscribe)

    result = await provider.generate_one(_req())
    assert calls["n"] == 2
    assert result.image.url == "https://fal.media/x.png"


@pytest.mark.asyncio
async def test_gives_up_after_max_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FalImageProvider(api_key="test")
    monkeypatch.setattr(provider, "_sleep_before_retry", AsyncMock())

    import fal_client

    async def always_fails(*args: object, **kwargs: object) -> dict[str, Any]:
        raise _http_error(500, "server error")

    monkeypatch.setattr(fal_client, "subscribe_async", always_fails)

    with pytest.raises(ProviderUnavailableError):
        await provider.generate_one(_req())


@pytest.mark.asyncio
async def test_content_filtered_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FalImageProvider(api_key="test")
    monkeypatch.setattr(provider, "_sleep_before_retry", AsyncMock())

    import fal_client

    calls = {"n": 0}

    async def fake_subscribe(*args: object, **kwargs: object) -> dict[str, Any]:
        calls["n"] += 1
        raise _http_error(400, "content filter triggered")

    monkeypatch.setattr(fal_client, "subscribe_async", fake_subscribe)

    with pytest.raises(ProviderContentFilteredError):
        await provider.generate_one(_req())
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_qwen_result_moderated_and_flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FalImageProvider(api_key="test")

    import fal_client

    async def fake_subscribe(
        model_id: str, *, arguments: dict[str, Any], **kwargs: object
    ) -> dict[str, Any]:
        if model_id == "fal-ai/x-ailab/nsfw":
            assert arguments["image_urls"] == ["https://fal.media/x.png"]
            return {"has_nsfw_concepts": [True]}
        return {
            "images": [{"url": "https://fal.media/x.png", "width": 1024, "height": 1024}],
            "seed": 7,
        }

    monkeypatch.setattr(fal_client, "subscribe_async", fake_subscribe)

    request = _req(
        style="poster",
        model_id="alibaba/qwen-image-3/text-to-image",
        param_profile="qwen",
    )
    result = await provider.generate_one(request)
    assert result.image.flagged is True


@pytest.mark.asyncio
async def test_qwen_moderation_failure_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FalImageProvider(api_key="test")

    import fal_client

    async def fake_subscribe(
        model_id: str, *, arguments: dict[str, Any], **kwargs: object
    ) -> dict[str, Any]:
        if model_id == "fal-ai/x-ailab/nsfw":
            raise RuntimeError("moderation endpoint down")
        return {
            "images": [{"url": "https://fal.media/x.png", "width": 1024, "height": 1024}],
            "seed": 7,
        }

    monkeypatch.setattr(fal_client, "subscribe_async", fake_subscribe)

    request = _req(
        style="poster",
        model_id="alibaba/qwen-image-3/text-to-image",
        param_profile="qwen",
    )
    result = await provider.generate_one(request)
    assert result.image.flagged is False


@pytest.mark.asyncio
async def test_flux_path_never_calls_moderation(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = FalImageProvider(api_key="test")

    import fal_client

    calls: list[str] = []

    async def fake_subscribe(
        model_id: str, *, arguments: dict[str, Any], **kwargs: object
    ) -> dict[str, Any]:
        calls.append(model_id)
        return _flux_result()

    monkeypatch.setattr(fal_client, "subscribe_async", fake_subscribe)

    await provider.generate_one(_req())
    assert calls == ["fal-ai/flux-2/flash"]
