"""Unit tests for Fal argument profiles (flux vs qwen)."""

from __future__ import annotations

from app.integrations.imagegen.fal import FalImageProvider
from app.integrations.imagegen.ports import ImageGenerationRequest


def _req(**overrides: object) -> ImageGenerationRequest:
    base: dict[str, object] = {
        "prompt": "a coffee cup",
        "style": "photo",
        "aspect": "square",
        "model_id": "fal-ai/flux/dev",
        "image_size": "square_hd",
        "param_profile": "flux",
        "enable_safety_checker": True,
        "num_inference_steps": 28,
    }
    base.update(overrides)
    return ImageGenerationRequest.model_validate(base)


def test_flux_arguments_include_safety_and_steps() -> None:
    provider = FalImageProvider(api_key="test")
    model_id, args = provider._build_call(_req())
    assert model_id == "fal-ai/flux/dev"
    assert args["enable_safety_checker"] is True
    assert args["num_inference_steps"] == 28
    assert "image_urls" not in args


def test_qwen_text_to_image_omits_flux_only_keys() -> None:
    provider = FalImageProvider(api_key="test")
    model_id, args = provider._build_call(
        _req(
            style="poster",
            model_id="alibaba/qwen-image-3/text-to-image",
            param_profile="qwen",
            num_inference_steps=None,
            enable_safety_checker=False,
        )
    )
    assert model_id == "alibaba/qwen-image-3/text-to-image"
    assert "enable_safety_checker" not in args
    assert "num_inference_steps" not in args
    assert "guidance_scale" not in args
    # Callers: pytest. User: variants almost exact → expansion disabled.
    assert args["enable_prompt_expansion"] is False
    assert "image_urls" not in args


def test_qwen_with_references_routes_to_edit() -> None:
    provider = FalImageProvider(api_key="test")
    model_id, args = provider._build_call(
        _req(
            style="poster",
            model_id="alibaba/qwen-image-3/text-to-image",
            param_profile="qwen",
            reference_image_urls=["https://cdn.test/logo.png"],
            num_inference_steps=None,
        )
    )
    assert model_id == "alibaba/qwen-image-3/edit"
    assert args["image_urls"] == ["https://cdn.test/logo.png"]
    assert "enable_safety_checker" not in args


def test_flux_accepts_explicit_pixel_size() -> None:
    provider = FalImageProvider(api_key="test")
    _, args = provider._build_call(
        _req(image_size={"width": 1080, "height": 1350}, num_inference_steps=None)
    )
    assert args["image_size"] == {"width": 1080, "height": 1350}


def test_ideogram_profile_arguments() -> None:
    provider = FalImageProvider(api_key="test")
    model_id, args = provider._build_call(
        _req(
            style="poster",
            model_id="fal-ai/ideogram/v3",
            param_profile="ideogram",
            image_size={"width": 1080, "height": 1080},
            num_inference_steps=None,
            enable_safety_checker=False,
        )
    )
    assert model_id == "fal-ai/ideogram/v3"
    assert args["rendering_speed"] == "BALANCED"
    assert args["image_size"] == {"width": 1080, "height": 1080}


def test_gpt_image_maps_portrait_to_enum() -> None:
    provider = FalImageProvider(api_key="test")
    model_id, args = provider._build_call(
        _req(
            model_id="fal-ai/gpt-image-1.5",
            param_profile="gpt_image",
            image_size={"width": 1080, "height": 1350},
            num_inference_steps=None,
            enable_safety_checker=False,
            quality_tier="standard",
        )
    )
    assert model_id == "fal-ai/gpt-image-1.5"
    assert args["image_size"] == "1024x1536"
    assert args["quality"] == "medium"
