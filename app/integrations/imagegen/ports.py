"""ImageGenerationProvider port — architecture/09 §1."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

VisualStyle = Literal["photo", "flat", "three-d", "minimal", "saudi-modern", "poster"]
VisualAspect = Literal["square", "portrait", "vertical", "landscape"]
ParamProfile = Literal["flux", "qwen"]


class ImageGenerationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    style: VisualStyle
    aspect: VisualAspect
    brand_color_hint: list[str] = Field(default_factory=list)
    model_id: str
    image_size: str
    param_profile: ParamProfile = "flux"
    reference_image_urls: list[str] = Field(default_factory=list, max_length=3)
    enable_safety_checker: bool = True
    num_inference_steps: int | None = None
    guidance_scale: float | None = None
    output_format: str = "png"
    seed: int | None = None


class GeneratedImage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    url: str
    width: int
    height: int
    seed: int | None = None
    flagged: bool = False


class GenerationUsage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cost_usd: Decimal
    provider_request_id: str | None = None
    latency_ms: int | None = None


class ImageGenerationResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    image: GeneratedImage
    usage: GenerationUsage
    raw: dict[str, object] | None = None


@runtime_checkable
class ImageGenerationProvider(Protocol):
    async def generate_one(
        self, request: ImageGenerationRequest
    ) -> ImageGenerationResult: ...
