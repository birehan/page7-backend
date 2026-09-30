"""Strategy regeneration prompt — strategy-v2."""

from __future__ import annotations

from typing import Any

from app.features.content_ai.prompts._shared import format_goals, format_guidelines, format_pillars
from app.features.strategy.schemas import StrategyOut
from app.integrations.llm.ports import LLMMessage

PROMPT_VERSION = "strategy-v2"

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "goals": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "text": {"type": "string"},
                    "progress": {"type": "number"},
                },
                "required": ["text", "progress"],
            },
        },
        "cadence": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "instagram": {"type": "number"},
                "facebook": {"type": "number"},
                "tiktok": {"type": "number"},
                "snapchat": {"type": "number"},
                "whatsapp": {"type": "number"},
            },
            "required": ["instagram", "facebook", "tiktok", "snapchat", "whatsapp"],
        },
    },
    "required": ["goals", "cadence"],
}


def build(*, brand: dict[str, Any], current: StrategyOut) -> list[LLMMessage]:
    guidelines = brand.get("guidelines") or {}
    goals = [g.model_dump(mode="json", by_alias=True) for g in current.goals]
    system = (
        "You propose an updated social strategy as JSON with goals and per-platform cadence. "
        "Do not apply changes — return a proposal only."
    )
    user = (
        f"Brand: {brand.get('name')}\nIndustry: {brand.get('industry')}\n"
        f"Current goals:\n{format_goals(goals)}\n"
        f"Pillars:\n{format_pillars(brand.get('pillars') or [])}\n"
        f"Guidelines:\n{format_guidelines(guidelines)}"
    )
    return [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content=user),
    ]
