"""Bilingual reply suggestion from recent thread context (reply-suggestion-v2)."""

from __future__ import annotations

from typing import Any, TypedDict

from app.integrations.llm.ports import LLMMessage

PROMPT_VERSION = "reply-suggestion-v2"

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "suggested_reply_ar": {"type": "string"},
        "suggested_reply_en": {"type": "string"},
    },
    "required": ["suggested_reply_ar", "suggested_reply_en"],
}


class TranscriptMessage(TypedDict):
    direction: str
    author_name: str
    body: str


def _format_transcript(messages: list[TranscriptMessage]) -> str:
    if not messages:
        return "(no messages)"
    lines: list[str] = []
    for msg in messages:
        side = "Customer" if msg["direction"] == "inbound" else "Brand"
        author = msg["author_name"].strip() or side
        body = msg["body"].strip() or "(empty)"
        lines.append(f"[{side} — {author}]: {body}")
    return "\n".join(lines)


def build(
    *,
    messages: list[TranscriptMessage],
    platform: str,
    kind: str,
    sentiment: str,
    brand_name: str | None = None,
) -> list[LLMMessage]:
    system = (
        "Suggest a short, professional reply as the brand in a social inbox thread. "
        "Use the full recent conversation for context (up to the last 20 messages). "
        "Reply to the latest customer need; do not repeat earlier brand replies. "
        "Return JSON only with suggested_reply_ar and suggested_reply_en. "
        "Keep each reply to one or two sentences."
    )
    brand_line = f"Brand: {brand_name}\n" if brand_name else ""
    user = (
        f"{brand_line}"
        f"Platform: {platform}\n"
        f"Kind: {kind}\n"
        f"Sentiment: {sentiment}\n"
        f"Recent messages (oldest to newest):\n{_format_transcript(messages)}\n"
    )
    return [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content=user),
    ]
