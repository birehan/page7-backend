"""Alt-text generation prompt."""

from __future__ import annotations

from typing import Any

from app.integrations.llm.ports import LLMMessage

PROMPT_VERSION = "alt-text-v1"

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "alt_ar": {"type": "string"},
        "alt_en": {"type": "string"},
    },
    "required": ["alt_ar", "alt_en"],
}


def build(*, caption: str | None) -> list[LLMMessage]:
    context = f"Caption context: {caption}" if caption else "No caption context."
    system = "Write concise, descriptive alt text in Arabic and English for accessibility."
    user = f"Describe the image for alt text. {context}"
    return [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content=user),
    ]
