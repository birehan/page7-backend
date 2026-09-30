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
    assert args["enable_prompt_expansion"] is True
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
