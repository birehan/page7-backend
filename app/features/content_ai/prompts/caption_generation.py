"""Caption generation prompt — copywriter-v5."""

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
from app.features.content_ai.prompts.corpus import load_caption_corpus
from app.integrations.llm.ports import LLMMessage

PROMPT_VERSION = "copywriter-v5"
FEW_SHOT_EXAMPLES = load_caption_corpus()
OUTPUT_SCHEMA = CAPTION_OUTPUT_SCHEMA


def build(
    *,
    brand: dict[str, Any],
    pillar: dict[str, Any] | None,
    cultural_event: dict[str, Any] | None,
    platforms: list[str],
    dialect: str,
    count: int,
    seed: dict[str, Any] | None = None,
) -> list[LLMMessage]:
    """Build caption-generation messages.

    The 24-theme corpus is deterministic mock filler used as voice/structure
    examples, not gold-standard human copy — see phase-07 Risks.
    """
    guidelines = brand.get("guidelines") or {}
    banned = guidelines.get("banned_claims") or guidelines.get("bannedClaims") or []
    seed_text = ""
    if seed:
        seed_text = (
            f"\nSeed AR: {seed.get('ar', '')}\nSeed EN: {seed.get('en', '')}\n"
            f"Seed hashtags: {seed.get('hashtags', [])}"
        )

    # Pin a compact few-shot set (first 4 themes) with {brand} filled.
    brand_name = str(brand.get("name") or "Brand")
    few_shot_lines: list[str] = []
    for example in FEW_SHOT_EXAMPLES[:4]:
        ar_key = "ar_gulf" if dialect == "gulf" else "ar_msa"
        ar = str(example.get(ar_key, "")).replace("{brand}", brand_name)
        en = str(example.get("en", "")).replace("{brand}", brand_name)
        few_shot_lines.append(f"- AR: {ar}\n  EN: {en}")
    few_shot_block = "\n".join(few_shot_lines)

    system = (
        "You write bilingual social captions for Saudi SMB brands. "
        f"Return exactly {count} variant(s) as JSON with ar/en captions and hashtags. "
        "Match the voice and structure of the few-shot examples; do not copy them verbatim."
    )
    user = (
        f"Brand: {brand.get('name')}\n"
        f"Industry: {brand.get('industry')}\nCity: {brand.get('city')}\n"
        f"Dialect: {dialect}\nPlatforms: {format_platforms(platforms)}\n"
        f"Pillar:\n{format_pillar(pillar)}\n"
        f"Cultural event:\n{format_cultural_event(cultural_event)}\n"
        f"Guidelines:\n{format_guidelines(guidelines)}\n"
        f"Banned claims:\n{format_banned_claims(list(banned))}\n"
        f"Few-shot examples (voice/structure only):\n{few_shot_block}\n"
        f"{seed_text}"
    )
    return [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content=user),
    ]
