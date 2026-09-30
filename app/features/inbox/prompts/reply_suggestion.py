"""Bilingual reply suggestion — both languages in one schema (reply-suggestion-v1)."""

from __future__ import annotations

from typing import Any

from app.integrations.llm.ports import LLMMessage

PROMPT_VERSION = "reply-suggestion-v1"

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "suggested_reply_ar": {"type": "string"},
        "suggested_reply_en": {"type": "string"},
    },
    "required": ["suggested_reply_ar", "suggested_reply_en"],
}


def build(
    *,
    body: str,
    platform: str,
    kind: str,
    sentiment: str,
    brand_name: str | None = None,
) -> list[LLMMessage]:
    system = (
        "Suggest a short, professional reply to a social inbox message. "
        "Return JSON only with suggested_reply_ar and suggested_reply_en. "
        "Keep each reply to one or two sentences."
    )
    brand_line = f"Brand: {brand_name}\n" if brand_name else ""
    user = (
        f"{brand_line}"
        f"Platform: {platform}\n"
        f"Kind: {kind}\n"
        f"Sentiment: {sentiment}\n"
        f"Inbound message:\n{body}\n"
    )
    return [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content=user),
    ]
