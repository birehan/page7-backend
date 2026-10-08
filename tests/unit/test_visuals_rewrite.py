"""Unit tests for visual brief rewrite (visuals.v2).

Callers: pytest. User: implement production AI image gen plan tests.
"""

from __future__ import annotations

from app.features.visuals.prompt_rewrite import (
    assemble_generation_prompt,
    contains_arabic,
    heuristic_brief,
    hex_to_color_name,
)


def test_heuristic_photo_strips_arabic_caption_from_scene() -> None:
    brief = heuristic_brief(
        caption_or_prompt="عرض خاص على القهوة اليوم في الرياض",
        style="photo",
        industry="cafe",
        city="Riyadh",
        color_names=["warm brown"],
        mode="photo",
    )
    assert not contains_arabic(brief.scene)
    assert "no text" in brief.scene.lower() or "no letters" in brief.negative_prompt
    assert brief.headline_suggestion == ""


def test_heuristic_poster_keeps_short_arabic_headline() -> None:
    brief = heuristic_brief(
        caption_or_prompt="عرض خاص اليوم على الوجبات العائلية",
        style="poster",
        industry="restaurant",
        mode="poster",
    )
    assert contains_arabic(brief.headline_suggestion)
    assert "poster" in brief.scene.lower()


def test_assemble_generation_prompt_photo_forbids_text() -> None:
    brief = heuristic_brief(
        caption_or_prompt="bright clinic reception with plants",
        style="photo",
        industry="clinic",
        mode="photo",
    )
    text = assemble_generation_prompt(
        brief,
        style_prefix="",
        style_suffix="professional photography",
        industry="clinic",
        city="Riyadh",
        headline=None,
        brand_colors_named=["fresh green"],
        use_brand_colors=True,
        saudi_policy=False,
        mode="photo",
    )
    assert "bright clinic" in text or "clinic" in text.lower()
    assert "no text" in text.lower() or "no letters" in text.lower()
    assert "fresh green" in text


def test_hex_to_color_name_basics() -> None:
    assert "black" in hex_to_color_name("#111111") or "near-black" in hex_to_color_name(
        "#111111"
    )
    assert "white" in hex_to_color_name("#FFFFFF") or "off-white" in hex_to_color_name(
        "#FFFFFF"
    )


def test_assemble_variant_prompts_are_distinct() -> None:
    from app.features.visuals.prompt_rewrite import assemble_variant_prompts

    brief = heuristic_brief(
        caption_or_prompt="bright clinic reception with plants",
        style="photo",
        industry="clinic",
        mode="photo",
    )
    prompts = assemble_variant_prompts(
        brief,
        count=3,
        style_prefix="",
        style_suffix="professional photography",
        industry="clinic",
        city="Riyadh",
        headline=None,
        brand_colors_named=["fresh green"],
        use_brand_colors=True,
        saudi_policy=False,
        mode="photo",
    )
    assert len(prompts) == 3
    assert len(set(prompts)) == 3
    assert prompts[0].startswith("ANGLE A")
    assert prompts[1].startswith("ANGLE B")
    assert "variation 1 of 3" in prompts[0]
    assert "variation 2 of 3" in prompts[1]
