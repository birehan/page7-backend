"""Unit tests for style/quality → fal model routing.

Callers: pytest. User: make Qwen Image 3 default for image generation.
"""

from __future__ import annotations

from app.core.config import _default_styles
from app.features.visuals.model_resolve import resolve_model


def test_photo_standard_uses_qwen() -> None:
    cfg = _default_styles()["photo"]
    model_id, profile = resolve_model(
        cfg, style="photo", quality="standard", headline=None, prompt="cafe"
    )
    assert model_id == "alibaba/qwen-image-3/text-to-image"
    assert profile == "qwen"


def test_photo_draft_uses_flash() -> None:
    cfg = _default_styles()["photo"]
    model_id, profile = resolve_model(
        cfg, style="photo", quality="draft", headline=None, prompt="cafe"
    )
    assert model_id == "fal-ai/flux-2/flash"
    assert profile == "flux"


def test_photo_premium_uses_ideogram() -> None:
    cfg = _default_styles()["photo"]
    model_id, profile = resolve_model(
        cfg, style="photo", quality="premium", headline=None, prompt="cafe"
    )
    assert model_id == "fal-ai/ideogram/v3"
    assert profile == "ideogram"


def test_poster_en_uses_ideogram() -> None:
    cfg = _default_styles()["poster"]
    model_id, profile = resolve_model(
        cfg,
        style="poster",
        quality="standard",
        headline="New Arrivals",
        prompt="skincare launch",
    )
    assert model_id == "fal-ai/ideogram/v3"
    assert profile == "ideogram"


def test_poster_ar_uses_qwen() -> None:
    cfg = _default_styles()["poster"]
    model_id, profile = resolve_model(
        cfg,
        style="poster",
        quality="standard",
        headline="عرض خاص",
        prompt="وجبات عائلية",
    )
    assert "qwen" in model_id
    assert profile == "qwen"
