"""Weekly insight report narrative prompt — insight-v1."""

from __future__ import annotations

import json
from typing import Any

from app.integrations.llm.ports import LLMMessage

PROMPT_VERSION = "insight-v1"

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "what_happened": {"type": "string"},
        "why": {"type": "string"},
        "what_to_change": {"type": "string"},
        "next_actions": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["what_happened", "why", "what_to_change", "next_actions"],
}


def build(
    *,
    brand: dict[str, Any],
    week_of: str,
    metrics: dict[str, Any],
    series: list[dict[str, Any]],
    pillar_breakdown: list[dict[str, Any]],
    platform_breakdown: list[dict[str, Any]],
    language: str,
) -> list[LLMMessage]:
    guidelines = brand.get("guidelines") or {}
    system = (
        "You write a concise weekly social analytics insight for an SMB marketer. "
        "Return JSON only with what_happened, why, what_to_change, and next_actions. "
        f"Write the narrative in language code '{language}'."
    )
    user = (
        f"Brand: {brand.get('name')}\n"
        f"Industry: {brand.get('industry')}\n"
        f"City: {brand.get('city')}\n"
        f"Week of (Riyadh Monday): {week_of}\n"
        f"Dialect: {guidelines.get('dialect')}\n"
        f"Metrics:\n{json.dumps(metrics, ensure_ascii=False, indent=2)}\n"
        f"Daily series:\n{json.dumps(series, ensure_ascii=False, indent=2)}\n"
        f"Pillar breakdown:\n{json.dumps(pillar_breakdown, ensure_ascii=False, indent=2)}\n"
        f"Platform breakdown:\n{json.dumps(platform_breakdown, ensure_ascii=False, indent=2)}\n"
    )
    return [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content=user),
    ]
