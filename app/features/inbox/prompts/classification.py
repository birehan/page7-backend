"""Inbound message sentiment classification — classification-v1."""

from __future__ import annotations

from typing import Any

from app.integrations.llm.ports import LLMMessage

PROMPT_VERSION = "classification-v1"

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "sentiment": {
            "type": "string",
            "enum": ["positive", "neutral", "negative"],
        },
    },
    "required": ["sentiment"],
}


def build(*, body: str, platform: str, kind: str) -> list[LLMMessage]:
    system = (
        "Classify the sentiment of a social inbox message for an SMB marketer. "
        "Return JSON only with sentiment one of positive, neutral, or negative."
    )
    user = (
        f"Platform: {platform}\n"
        f"Kind: {kind}\n"
        f"Message:\n{body}\n"
    )
    return [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content=user),
    ]
