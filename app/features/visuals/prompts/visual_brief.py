"""LLM prompt + schema for visual brief rewrite (visuals.v2).

Callers: features/visuals/prompt_rewrite.py.
User: implement production AI image gen plan — prompt rewriter.
"""

from __future__ import annotations

from typing import Any

from app.integrations.llm.ports import LLMMessage

PROMPT_VERSION = "visuals.v2"

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "scene": {
            "type": "string",
            "description": "English photographic/illustration scene description, no marketing copy",
        },
        "subjects": {"type": "string"},
        "lighting": {"type": "string"},
        "mood": {"type": "string"},
        "must_avoid": {"type": "string"},
        "negative_prompt": {"type": "string"},
        "headline_suggestion": {
            "type": "string",
            "description": "Short poster headline (max ~8 words) or empty for photo mode",
        },
        "overlay_hint": {
            "type": "string",
            "enum": ["logo_bottom_left", "logo_bottom_right", "none"],
        },
    },
    "required": [
        "scene",
        "subjects",
        "lighting",
        "mood",
        "must_avoid",
        "negative_prompt",
        "headline_suggestion",
        "overlay_hint",
    ],
}


def build_messages(
    *,
    caption_or_prompt: str,
    style: str,
    aspect: str,
    mode: str,
    brand_name: str | None,
    industry: str | None,
    city: str | None,
    color_names: list[str],
    language_hint: str,
) -> list[LLMMessage]:
    system = (
        "You rewrite social-media captions into image-generation briefs for "
        "Instagram/Facebook creatives aimed at the Saudi market. "
        "Output JSON only matching the schema. "
        "CRITICAL: scene/subjects/lighting/mood must be English visual description — "
        "never paste Arabic or Latin marketing slogans into scene fields. "
        "Photo mode: absolutely no readable text, letters, logos, or watermarks in the scene. "
        "Poster mode: scene is a clean poster background; put short copy only in "
        "headline_suggestion (may be Arabic or English matching the user's language). "
        "Saudi-appropriate: no alcohol, no pork, modest dress."
    )
    colors = ", ".join(color_names) if color_names else "none"
    user = (
        f"mode={mode}\n"
        f"style={style}\n"
        f"aspect={aspect}\n"
        f"language_hint={language_hint}\n"
        f"brand_name={brand_name or 'unknown'}\n"
        f"industry={industry or 'general'}\n"
        f"city={city or 'unspecified'}\n"
        f"brand_colors={colors}\n"
        f"user_input:\n{caption_or_prompt.strip()}"
    )
    return [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content=user),
    ]
