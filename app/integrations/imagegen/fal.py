"""Fal.ai ImageGenerationProvider adapter (architecture/09 §1).

Only this module may import fal_client (import-linter contract).
Supports flux-family and Qwen Image 3 argument profiles.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import Any

import fal_client
from fal_client import FalClientError, FalClientHTTPError, FalClientTimeoutError

from app.infrastructure.telemetry.metrics import provider_call
from app.integrations.errors import (
    ProviderAuthError,
    ProviderContentFilteredError,
    ProviderPaymentRequiredError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.integrations.imagegen.cost import estimate_image_cost_usd
from app.integrations.imagegen.ports import (
    GeneratedImage,
    GenerationUsage,
    ImageGenerationRequest,
    ImageGenerationResult,
)

_QWEN_TEXT_TO_IMAGE = "alibaba/qwen-image-3/text-to-image"
_QWEN_EDIT = "alibaba/qwen-image-3/edit"
# architecture/09 review §6.4/§7: Qwen (poster) has no has_nsfw_concepts signal at
# all — safety was prompt-policy only. This closes that gap with a cheap
# ($0.001/image), same-fal_client moderation pass run only on the Qwen path.
_NSFW_MODERATION_MODEL = "fal-ai/x-ailab/nsfw"

# architecture/09 review §7: fal's own production guidance requires client-side
# retry/backoff for transient 429/5xx before a caller records a failure. Capped
# at 2 retries (3 attempts total) so one slow image can't blow the SSE budget.
_MAX_RETRIES = 2
_BACKOFF_SECONDS = (1.0, 4.0)


class FalImageProvider:
    """Wraps fal_client.subscribe_async; maps SDK errors onto Provider* types."""

    def __init__(self, *, api_key: str, timeout_seconds: float = 120.0) -> None:
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    async def generate_one(
        self, request: ImageGenerationRequest
    ) -> ImageGenerationResult:
        model_id, arguments = self._build_call(request)
        started = time.perf_counter()
        result = await self._subscribe_with_retries(model_id, arguments)
        latency_ms = int((time.perf_counter() - started) * 1000)
        parsed = self._parse_result(
            request, result, latency_ms=latency_ms, model_id=model_id
        )
        if request.param_profile == "qwen" and not parsed.image.flagged:
            parsed = await self._moderate_qwen_result(parsed)
        return parsed

    async def _moderate_qwen_result(
        self, result: ImageGenerationResult
    ) -> ImageGenerationResult:
        """Best-effort NSFW check on the Qwen output (architecture/09 review §6.4).

        A moderation-call failure must never block a real generation from
        reaching the user — it fails open (unflagged) and relies on the
        existing prompt-side Saudi-appropriateness policy as before, exactly
        matching the documented pre-existing gap rather than introducing a
        new failure mode.
        """
        try:
            async with provider_call(provider="fal", operation="moderate_qwen"):
                moderation = await fal_client.subscribe_async(
                    _NSFW_MODERATION_MODEL,
                    arguments={"image_urls": [result.image.url]},
                    client_timeout=30.0,
                )
        except Exception:  # noqa: BLE001 — fail open, see docstring
            return result
        if not isinstance(moderation, dict):
            return result
        flags = moderation.get("has_nsfw_concepts")
        flagged = bool(flags[0]) if isinstance(flags, list) and flags else False
        if not flagged:
            return result
        return result.model_copy(
            update={"image": result.image.model_copy(update={"flagged": True})}
        )

    async def _subscribe_with_retries(
        self, model_id: str, arguments: dict[str, Any]
    ) -> object:
        attempt = 0
        while True:
            async with provider_call(provider="fal", operation="generate_one"):
                try:
                    return await fal_client.subscribe_async(
                        model_id,
                        arguments=arguments,
                        client_timeout=self._timeout_seconds,
                    )
                except Exception as exc:
                    mapped = _map_fal_error(exc)
                    retryable = isinstance(
                        mapped, (ProviderRateLimitedError, ProviderUnavailableError)
                    )
                    if not retryable or attempt >= _MAX_RETRIES:
                        raise mapped from exc
                    retry_after = getattr(mapped, "retry_after", None)
                    await self._sleep_before_retry(retry_after, attempt)
                    attempt += 1

    async def _sleep_before_retry(
        self, retry_after: float | None, attempt: int
    ) -> None:
        delay = (
            retry_after
            if retry_after is not None
            else _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
        )
        delay += random.uniform(0, 0.5)  # noqa: S311
        await asyncio.sleep(delay)

    def _build_call(
        self, request: ImageGenerationRequest
    ) -> tuple[str, dict[str, Any]]:
        if request.param_profile == "qwen":
            return self._build_qwen_call(request)
        return request.model_id, self._build_flux_arguments(request)

    def _build_flux_arguments(self, request: ImageGenerationRequest) -> dict[str, Any]:
        args: dict[str, Any] = {
            "prompt": request.prompt,
            "image_size": request.image_size,
            "enable_safety_checker": request.enable_safety_checker,
            "output_format": request.output_format,
            "num_images": 1,
        }
        if request.num_inference_steps is not None:
            args["num_inference_steps"] = request.num_inference_steps
        if request.guidance_scale is not None:
            args["guidance_scale"] = request.guidance_scale
        if request.seed is not None:
            args["seed"] = request.seed
        return args

    def _build_qwen_call(
        self, request: ImageGenerationRequest
    ) -> tuple[str, dict[str, Any]]:
        refs = list(request.reference_image_urls)[:3]
        args: dict[str, Any] = {
            "prompt": request.prompt,
            "image_size": request.image_size,
            "num_images": 1,
            "output_format": request.output_format,
            "enable_prompt_expansion": True,
        }
        if request.seed is not None:
            args["seed"] = request.seed
        if refs:
            args["image_urls"] = refs
            return _QWEN_EDIT, args
        model = (
            request.model_id
            if request.model_id.startswith("alibaba/qwen-image-3")
            else _QWEN_TEXT_TO_IMAGE
        )
        return model, args

    def _parse_result(
        self,
        request: ImageGenerationRequest,
        result: object,
        *,
        latency_ms: int,
        model_id: str,
    ) -> ImageGenerationResult:
        if not isinstance(result, dict):
            raise ProviderUnavailableError("fal returned a non-object result")

        images = result.get("images")
        if not isinstance(images, list) or not images:
            raise ProviderUnavailableError("fal result missing images")

        first = images[0]
        if not isinstance(first, dict):
            raise ProviderUnavailableError("fal image entry is not an object")

        url = first.get("url")
        width = first.get("width")
        height = first.get("height")
        # Qwen sometimes omits width/height — default from aspect map conventions.
        if not isinstance(width, int) or not isinstance(height, int):
            width, height = _default_dims(request.image_size)
        if not isinstance(url, str):
            raise ProviderUnavailableError("fal image missing url")

        flagged = _extract_flagged(result, index=0, profile=request.param_profile)
        seed_raw = result.get("seed")
        seed = seed_raw if isinstance(seed_raw, int) else None

        cost = estimate_image_cost_usd(model_id, width, height)
        request_id = result.get("request_id")
        provider_request_id = request_id if isinstance(request_id, str) else None

        return ImageGenerationResult(
            image=GeneratedImage(
                url=url,
                width=width,
                height=height,
                seed=seed,
                flagged=flagged,
            ),
            usage=GenerationUsage(
                cost_usd=cost,
                provider_request_id=provider_request_id,
                latency_ms=latency_ms,
            ),
            raw=dict(result),
        )


def _default_dims(image_size: str) -> tuple[int, int]:
    defaults: dict[str, tuple[int, int]] = {
        "square_hd": (1024, 1024),
        "portrait_4_3": (768, 1024),
        "portrait_16_9": (576, 1024),
        "landscape_16_9": (1024, 576),
    }
    return defaults.get(image_size, (1024, 1024))


def _extract_flagged(
    result: dict[str, Any], *, index: int, profile: str = "flux"
) -> bool:
    """architecture/09 §5: missing has_nsfw_concepts ⇒ unflagged for flux.

    Qwen's own generation response never returns has_nsfw_concepts; this
    always returns False for profile="qwen" and callers must run a separate
    moderation pass (see FalImageProvider._moderate_qwen_result) rather than
    look for a signal in this response.
    """
    if profile == "qwen":
        return False
    nsfw = result.get("has_nsfw_concepts")
    if nsfw is None:
        return False
    if isinstance(nsfw, list):
        if index >= len(nsfw):
            return False
        return bool(nsfw[index])
    if isinstance(nsfw, bool):
        return nsfw
    return False


def _map_fal_error(exc: Exception) -> Exception:
    if isinstance(exc, FalClientTimeoutError):
        return ProviderTimeoutError(str(exc))
    if isinstance(exc, FalClientHTTPError):
        status = getattr(exc, "status_code", None)
        if status == 401:
            return ProviderAuthError(str(exc))
        if status == 402:
            return ProviderPaymentRequiredError(str(exc))
        if status == 429:
            return ProviderRateLimitedError()
        if status is not None and status >= 500:
            return ProviderUnavailableError(str(exc))
        message = str(exc).lower()
        if "content" in message and "filter" in message:
            return ProviderContentFilteredError(str(exc))
        return ProviderUnavailableError(str(exc))
    if isinstance(exc, FalClientError):
        return ProviderUnavailableError(str(exc))
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if "timeout" in name or "timeout" in message:
        return ProviderTimeoutError(str(exc))
    return ProviderUnavailableError(str(exc))
