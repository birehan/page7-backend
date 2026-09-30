"""Caption transform prompt — copywriter-v5."""

from __future__ import annotations

from typing import Any

from app.features.content_ai.prompts._caption_schema import CAPTION_OUTPUT_SCHEMA
from app.features.content_ai.prompts._shared import (
    format_banned_claims,
    format_cultural_event,
    format_guidelines,
    format_pillar,
    format_platforms,
)
from app.integrations.llm.ports import LLMMessage

PROMPT_VERSION = "copywriter-v5"
OUTPUT_SCHEMA = CAPTION_OUTPUT_SCHEMA


def build(
    *,
    brand: dict[str, Any],
    pillar: dict[str, Any] | None,
    cultural_event: dict[str, Any] | None,
    platforms: list[str],
    dialect: str,
    intent: str,
    count: int,
    seed: dict[str, Any],
) -> list[LLMMessage]:
    guidelines = brand.get("guidelines") or {}
    banned = guidelines.get("banned_claims") or guidelines.get("bannedClaims") or []
    system = (
        "You transform bilingual social captions for Saudi SMB brands. "
        f"Apply intent '{intent}'. Return {count} variant(s) as JSON."
    )
    user = (
        f"Brand: {brand.get('name')}\nDialect: {dialect}\n"
        f"Platforms: {format_platforms(platforms)}\nIntent: {intent}\n"
        f"Pillar:\n{format_pillar(pillar)}\n"
        f"Cultural event:\n{format_cultural_event(cultural_event)}\n"
        f"Guidelines:\n{format_guidelines(guidelines)}\n"
        f"Banned claims:\n{format_banned_claims(list(banned))}\n"
        f"Seed AR: {seed.get('ar', '')}\nSeed EN: {seed.get('en', '')}\n"
        f"Seed hashtags: {seed.get('hashtags', [])}"
    )
    return [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content=user),
    ]
