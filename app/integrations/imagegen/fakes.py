"""Fake ImageGenerationProvider — zero network I/O (architecture/06 §5)."""

from __future__ import annotations

from app.integrations.errors import (
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

# Deterministic dimensions matching fal square_hd / portrait_4_3 / etc.
_ASPECT_DIMS: dict[str, tuple[int, int]] = {
    "square_hd": (1024, 1024),
    "portrait_4_3": (768, 1024),
    "portrait_16_9": (576, 1024),
    "landscape_16_9": (1024, 576),
}

# Valid 1×1 RGB PNG — browsers reject the old signature-only stub that left the
# generate grid blank in fake mode.
FAKE_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\xdac```\x00\x00"
    b"\x00\x04\x00\x01\xc8\xea\xeb\xf9\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeImageGenerationProvider:
    """Deterministic canned images with optional per-index failure injection."""

    def __init__(
        self,
        *,
        flagged_indexes: set[int] | None = None,
        timeout_indexes: set[int] | None = None,
        unavailable_indexes: set[int] | None = None,
        omit_nsfw_signal: bool = False,
    ) -> None:
        self.calls: list[ImageGenerationRequest] = []
        self.flagged_indexes = flagged_indexes or set()
        self.timeout_indexes = timeout_indexes or set()
        self.unavailable_indexes = unavailable_indexes or set()
        self.omit_nsfw_signal = omit_nsfw_signal
        self._call_index = 0

    async def generate_one(
        self, request: ImageGenerationRequest
    ) -> ImageGenerationResult:
        idx = self._call_index
        self._call_index += 1
        self.calls.append(request)

        if idx in self.timeout_indexes:
            raise ProviderTimeoutError("fake timeout")
        if idx in self.unavailable_indexes:
            raise ProviderUnavailableError("fake unavailable")

        width, height = _ASPECT_DIMS.get(request.image_size, (1024, 1024))
        flagged = idx in self.flagged_indexes and not self.omit_nsfw_signal

        cost = estimate_image_cost_usd(request.model_id, width, height)
        return ImageGenerationResult(
            image=GeneratedImage(
                url=f"https://fal.media/files/fake/{idx}.png",
                width=width,
                height=height,
                seed=42 + idx,
                flagged=flagged,
            ),
            usage=GenerationUsage(
                cost_usd=cost,
                provider_request_id=f"fake-req-{idx}",
                latency_ms=50,
            ),
            raw={"fake": True, "index": idx},
        )

    def reset(self) -> None:
        self.calls.clear()
        self._call_index = 0
