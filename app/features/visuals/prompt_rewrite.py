"""Rewrite captions into Flux/Ideogram-safe visual briefs (visuals.v2).

Callers: features/visuals/service.py start_generate; scripts/imagegen_bakeoff.
API: POST /visuals/generate uses rewritten prompt instead of raw caption.
Schema: VisualBrief pydantic model.
User instruction: implement the plan — prompt rewriter; stop feeding raw Arabic into photo models.
"""

from __future__ import annotations

import re
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict

from app.features.visuals.prompts import visual_brief as brief_prompt
from app.integrations.llm.ports import StructuredGenerationRequest
from app.integrations.llm.router import LLMTaskRouter

log = structlog.get_logger(__name__)

PROMPT_VERSION = brief_prompt.PROMPT_VERSION

_ARABIC_SCRIPT = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")

_NO_TEXT = (
    "absolutely no text anywhere: no letters, no numbers, no words, no titles, "
    "no diagrams with labels, blank paper only, screens show soft abstract color "
    "glow with zero readable characters, no logos, no watermarks"
)

# Distinct camera/composition framings so count>1 is not near-duplicates.
# Callers: assemble_variant_prompts (visuals.service). User: variants almost exact.
_VARIATION_FRAMES: tuple[str, ...] = (
    (
        "ANGLE A — ultra-wide establishing shot from the doorway, "
        "lots of empty floor in the foreground, cool daylight from the left"
    ),
    (
        "ANGLE B — medium portrait framing, subject fills the center, "
        "shallow depth of field, soft frontal key light"
    ),
    (
        "ANGLE C — extreme detail close-up of textures and materials, "
        "macro-like, warm side light, background fully abstract"
    ),
    (
        "ANGLE D — over-the-shoulder lifestyle angle looking into the space, "
        "golden-hour warmth, people in modest dress as soft silhouettes"
    ),
)

_INDUSTRY_SCENES: dict[str, str] = {
    "restaurant": (
        "editorial food photograph of a plated Saudi-inspired dish on a clean table, "
        "shallow depth of field, warm daylight"
    ),
    "cafe": (
        "lifestyle cafe interior with latte art cup on wood counter, soft morning light, "
        "blurred patrons in modest dress"
    ),
    "salon": (
        "modern beauty salon station with neat tools and soft pastel towels, "
        "bright clean lighting, no faces with logos"
    ),
    "clinic": (
        "bright modern clinic reception with plants and soft daylight, "
        "calm professional atmosphere"
    ),
    "dental": (
        "bright modern dental clinic reception with plants and soft daylight, "
        "calm professional atmosphere"
    ),
    "retail": (
        "premium retail product flat-lay on neutral surface with soft shadows, "
        "studio lighting"
    ),
    "fashion": (
        "editorial fashion still-life of fabric textures and accessories, "
        "softbox lighting, no readable labels"
    ),
    "real estate": (
        "bright contemporary living room interior with natural window light, "
        "minimal staging"
    ),
    "tech": (
        "clean desk lifestyle shot with laptop showing soft abstract glow only, "
        "city dusk through window"
    ),
    "services": (
        "professional workspace lifestyle photograph, soft natural light, "
        "authentic materials"
    ),
}

OverlayHint = Literal["logo_bottom_left", "logo_bottom_right", "none"]


class VisualBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scene: str
    subjects: str = ""
    lighting: str = ""
    mood: str = ""
    must_avoid: str = ""
    negative_prompt: str = ""
    headline_suggestion: str = ""
    overlay_hint: OverlayHint = "logo_bottom_left"


def contains_arabic(text: str) -> bool:
    return bool(_ARABIC_SCRIPT.search(text or ""))


def detect_language_hint(text: str) -> str:
    return "ar" if contains_arabic(text) else "en"


def hex_to_color_name(hex_color: str) -> str:
    """Map a hex to a rough English color name for prompts (models ignore raw #hex)."""
    raw = hex_color.strip().lstrip("#")
    if len(raw) == 3:
        raw = "".join(c * 2 for c in raw)
    if len(raw) != 6:
        return "accent color"
    try:
        r, g, b = int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    except ValueError:
        return "accent color"
    mx = max(r, g, b)
    mn = min(r, g, b)
    if mx - mn < 30:
        if mx < 40:
            return "near-black"
        if mx > 220:
            return "off-white"
        return "neutral gray"
    if r >= g and r >= b:
        return "warm red" if r - g > 40 else "warm orange"
    if g >= r and g >= b:
        return "fresh green" if g - b > 40 else "teal green"
    return "deep blue" if b - r > 40 else "cool purple"


def color_names_from_hex(colors: list[str]) -> list[str]:
    return [hex_to_color_name(c) for c in colors[:6]]


def _industry_scene(industry: str | None) -> str:
    if not industry:
        return _INDUSTRY_SCENES["services"]
    key = industry.strip().lower()
    for token, scene in _INDUSTRY_SCENES.items():
        if token in key:
            return scene
    return (
        f"photorealistic Instagram photograph related to {industry.strip()}, "
        "authentic materials, shallow depth of field"
    )


def heuristic_brief(
    *,
    caption_or_prompt: str,
    style: str,
    industry: str | None = None,
    city: str | None = None,
    color_names: list[str] | None = None,
    mode: str | None = None,
) -> VisualBrief:
    """Deterministic fallback when LLM is unavailable — never feeds Arabic into scene."""
    resolved_mode = mode or ("poster" if style == "poster" else "photo")
    latin_safe = not contains_arabic(caption_or_prompt)
    user_visual = caption_or_prompt.strip() if latin_safe else ""
    scene_core = (
        user_visual if user_visual and len(user_visual) > 12 else _industry_scene(industry)
    )
    city_bit = f", set in {city.strip()}" if city and not contains_arabic(city) else ""
    palette = ""
    if color_names:
        palette = f", color accents of {', '.join(color_names[:3])}"

    if resolved_mode == "poster":
        scene = (
            f"clean social media poster background, {scene_core}{city_bit}{palette}, "
            "generous negative space for headline typography, Saudi-market appropriate"
        )
        headline = ""
        if contains_arabic(caption_or_prompt):
            headline = caption_or_prompt.strip()[:40]
        elif caption_or_prompt.strip():
            words = caption_or_prompt.strip().split()
            headline = " ".join(words[:6])
        return VisualBrief(
            scene=scene,
            subjects="brand graphic layout",
            lighting="even studio lighting",
            mood="polished marketing",
            must_avoid="clutter, illegible text, alcohol, pork",
            negative_prompt="blurry text, watermark, low contrast, busy background",
            headline_suggestion=headline,
            overlay_hint="logo_bottom_left",
        )

    scene = (
        f"photorealistic Instagram portrait photograph, {scene_core}{city_bit}"
        f"{palette}, authentic materials, shallow depth of field. {_NO_TEXT}"
    )
    return VisualBrief(
        scene=scene,
        subjects="lifestyle scene",
        lighting="natural soft light",
        mood="premium and inviting",
        must_avoid="text, letters, logos, watermarks, alcohol, pork",
        negative_prompt=(
            "text, letters, typography, watermark, logo, caption, signage, "
            "alcohol, pork, immodest dress"
        ),
        headline_suggestion="",
        overlay_hint="logo_bottom_left",
    )


def assemble_generation_prompt(
    brief: VisualBrief,
    *,
    style_prefix: str,
    style_suffix: str,
    industry: str | None,
    city: str | None,
    headline: str | None,
    brand_colors_named: list[str],
    use_brand_colors: bool,
    saudi_policy: bool,
    mode: str,
) -> str:
    """Build the final string sent to the image model from a VisualBrief."""
    parts: list[str] = []
    if style_prefix.strip():
        parts.append(style_prefix.strip())
    parts.append(brief.scene.strip())
    if brief.subjects.strip():
        parts.append(f"subjects: {brief.subjects.strip()}")
    if brief.lighting.strip():
        parts.append(f"lighting: {brief.lighting.strip()}")
    if brief.mood.strip():
        parts.append(f"mood: {brief.mood.strip()}")
    effective_headline = (headline or brief.headline_suggestion or "").strip()
    if mode == "poster" and effective_headline:
        parts.append(f'render this headline text clearly: "{effective_headline}"')
    if industry and industry.strip():
        parts.append(f"brand industry: {industry.strip()}")
    if city and city.strip() and not contains_arabic(city):
        parts.append(f"city: {city.strip()}")
    if style_suffix.strip():
        parts.append(style_suffix.strip())
    if use_brand_colors and brand_colors_named:
        parts.append("brand palette: " + ", ".join(brand_colors_named))
    if saudi_policy or mode == "poster":
        parts.append(
            "Saudi-market appropriate imagery: no alcohol, no pork, modest dress"
        )
    if mode != "poster":
        parts.append(_NO_TEXT)
    return ". ".join(p for p in parts if p)


def assemble_variant_prompts(
    brief: VisualBrief,
    *,
    count: int,
    style_prefix: str,
    style_suffix: str,
    industry: str | None,
    city: str | None,
    headline: str | None,
    brand_colors_named: list[str],
    use_brand_colors: bool,
    saudi_policy: bool,
    mode: str,
) -> list[str]:
    """Build N distinct prompts (different camera frames) for batch diversity."""
    base = assemble_generation_prompt(
        brief,
        style_prefix=style_prefix,
        style_suffix=style_suffix,
        industry=industry,
        city=city,
        headline=headline,
        brand_colors_named=brand_colors_named,
        use_brand_colors=use_brand_colors,
        saudi_policy=saudi_policy,
        mode=mode,
    )
    if count <= 1:
        return [base]
    prompts: list[str] = []
    for i in range(count):
        frame = _VARIATION_FRAMES[i % len(_VARIATION_FRAMES)]
        # Lead with the angle so seed+prompt both diverge before the shared scene.
        prompts.append(
            f"{frame}. variation {i + 1} of {count}. scene: {base}"
        )
    return prompts


async def rewrite_visual_brief(
    *,
    caption_or_prompt: str,
    style: str,
    aspect: str,
    brand_name: str | None,
    industry: str | None,
    city: str | None,
    brand_colors: list[str],
    router: LLMTaskRouter | None,
    use_llm: bool = True,
) -> VisualBrief:
    """LLM rewrite with heuristic fallback. Always returns a usable brief."""
    mode = "poster" if style == "poster" else "photo"
    names = color_names_from_hex(brand_colors)
    language_hint = detect_language_hint(caption_or_prompt)
    fallback = heuristic_brief(
        caption_or_prompt=caption_or_prompt,
        style=style,
        industry=industry,
        city=city,
        color_names=names,
        mode=mode,
    )
    if not use_llm or router is None:
        return fallback

    messages = brief_prompt.build_messages(
        caption_or_prompt=caption_or_prompt,
        style=style,
        aspect=aspect,
        mode=mode,
        brand_name=brand_name,
        industry=industry,
        city=city,
        color_names=names,
        language_hint=language_hint,
    )
    request = StructuredGenerationRequest(
        messages=messages,
        output_schema=brief_prompt.OUTPUT_SCHEMA,
        schema_name="visual_brief",
        temperature=0.4,
    )
    try:
        response = await router.run_structured("visual_brief", request)
        content: dict[str, Any] = dict(response.content or {})
        if mode == "photo":
            for key in ("scene", "subjects", "lighting", "mood"):
                val = content.get(key)
                if isinstance(val, str) and contains_arabic(val):
                    content[key] = getattr(fallback, key)
            content["headline_suggestion"] = ""
        return VisualBrief.model_validate(content)
    except Exception as exc:  # noqa: BLE001 — fall back; generation must not fail
        log.warning("visual_brief_rewrite_failed", error=str(exc))
        return fallback
