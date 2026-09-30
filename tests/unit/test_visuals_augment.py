"""Unit tests for visuals prompt augmentation."""

from __future__ import annotations

from app.core.config import _default_styles
from app.features.visuals.service import augment_prompt


def test_augment_prompt_includes_headline_industry_and_saudi_policy() -> None:
    text = augment_prompt(
        prompt="coffee shop offer",
        style_prefix="",
        style_suffix="clean poster",
        brand_colors=["#111"],
        use_brand_colors=True,
        industry="F&B",
        city="Riyadh",
        headline="عرض خاص",
        saudi_policy=True,
    )
    assert "coffee shop offer" in text
    assert 'render this headline text clearly: "عرض خاص"' in text
    assert "brand industry: F&B" in text
    assert "city: Riyadh" in text
    assert "brand palette: #111" in text
    assert "no alcohol" in text


def test_flux_style_suffixes_forbid_in_image_text() -> None:
    styles = _default_styles()
    for name, cfg in styles.items():
        if name == "poster":
            assert "no text" not in cfg.prompt_suffix
            continue
        assert "no text" in cfg.prompt_suffix, name
        assert "no letters" in cfg.prompt_suffix, name
