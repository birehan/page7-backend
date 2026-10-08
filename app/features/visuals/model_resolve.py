"""Resolve fal model_id + param_profile from style, quality, and language.

Callers: features/visuals/service.py. User: fal-profiles + generate-ui quality.
"""

from __future__ import annotations

from typing import Literal

from app.core.config import StyleConfig
from app.features.visuals.prompt_rewrite import contains_arabic
from app.integrations.imagegen.ports import ParamProfile

Quality = Literal["draft", "standard", "premium"]


def resolve_model(
    style_cfg: StyleConfig,
    *,
    style: str,
    quality: Quality,
    headline: str | None,
    prompt: str,
) -> tuple[str, ParamProfile]:
    """Pick model + profile for this generation."""
    if quality == "draft" and style_cfg.draft_model_id:
        profile = style_cfg.draft_param_profile or style_cfg.param_profile
        return style_cfg.draft_model_id, profile  # type: ignore[return-value]

    if quality == "premium" and style_cfg.premium_model_id:
        profile = style_cfg.premium_param_profile or "gpt_image"
        return style_cfg.premium_model_id, profile  # type: ignore[return-value]

    # Poster EN → Ideogram when configured; AR → Qwen (default model_id).
    if style == "poster" and style_cfg.en_model_id and style_cfg.en_param_profile:
        text = f"{headline or ''} {prompt}"
        if not contains_arabic(text):
            return style_cfg.en_model_id, style_cfg.en_param_profile  # type: ignore[return-value]

    return style_cfg.model_id, style_cfg.param_profile  # type: ignore[return-value]
