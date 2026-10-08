"""Plan generation prompt — planner-v8."""

from __future__ import annotations

from datetime import date
from typing import Any

from app.features.content_ai.prompts._shared import (
    format_cadence,
    format_cultural_events,
    format_guidelines,
    format_pillars,
)
from app.integrations.llm.ports import LLMMessage

PROMPT_VERSION = "planner-v8"

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string"},
                    "platform": {
                        "type": "string",
                        "enum": [
                            "instagram",
                            "facebook",
                            "tiktok",
                            "snapchat",
                            "whatsapp",
                        ],
                    },
                    "day_offset": {"type": "integer"},
                    "scheduled_time": {
                        "type": "string",
                        "description": "24-hour local time as HH:MM, e.g. '14:00'. No date, seconds, or timezone.",
                        "pattern": "^([01][0-9]|2[0-3]):[0-5][0-9]$",
                    },
                    "text_ar": {
                        "type": "string",
                        "description": (
                            "Arabic caption in Arabic script only (full sentences). "
                            "Never empty. Never Latin-only English."
                        ),
                    },
                    "text_en": {
                        "type": "string",
                        "description": (
                            "English caption in Latin script only (full sentences). "
                            "Never empty. Never Arabic script."
                        ),
                    },
                    "hashtags_ar": {"type": "array", "items": {"type": "string"}},
                    "hashtags_en": {"type": "array", "items": {"type": "string"}},
                    "pillar_index": {"type": "integer"},
                },
                "required": [
                    "title",
                    "platform",
                    "day_offset",
                    "scheduled_time",
                    "text_ar",
                    "text_en",
                    "hashtags_ar",
                    "hashtags_en",
                    "pillar_index",
                ],
            },
        },
    },
    "required": ["items"],
}


def build(
    *,
    brand: dict[str, Any],
    from_date: date,
    to_date: date,
    cadence: dict[str, int],
    only_gaps: bool,
    cultural_events: list[dict[str, Any]],
    existing_summary: str,
    target_count: int | None = None,
) -> list[LLMMessage]:
    guidelines = brand.get("guidelines") or {}
    span_days = (to_date - from_date).days
    max_offset = max(span_days, 0)
    inclusive_days = max_offset + 1
    weekly_total = sum(max(int(v), 0) for v in cadence.values())
    if weekly_total:
        cadence_target = max(1, round(weekly_total * inclusive_days / 7))
    else:
        cadence_target = inclusive_days

    languages = guidelines.get("languages") or []
    primary_lang = "ar"
    if isinstance(languages, list) and languages:
        candidate = str(languages[0]).lower().strip()
        if candidate in {"ar", "en"}:
            primary_lang = candidate
    other_lang = "en" if primary_lang == "ar" else "ar"
    system = (
        "You draft a social content calendar as JSON. "
        "Each item needs bilingual captions and respects cadence per platform. "
        f"Primary language is {primary_lang}: write the strongest original copy in "
        f"text_{primary_lang}; text_{other_lang} is a faithful translation. "
        "CRITICAL language rules: text_en must be English in Latin letters only "
        "(no Arabic letters). text_ar must be Arabic in Arabic script "
        "(not transliteration, not English). Never swap the slots. "
        "Never put Arabic into text_en. Never put English-only copy into text_ar. "
        "Ground every caption in the brand pillars — the real product or service. "
        "Avoid generic corporate tropes (Behind the Scenes, Customer Love, "
        "motivational fluff) unless they clearly fit the pillar. "
        "Never leave text_ar or text_en empty — always provide both. "
        "Instagram captions: 2–4 short sentences (spoken voice). "
        "Hook + one concrete proof of the offer + soft CTA. "
        "No markdown, no bullet lists, no walls of text. "
        "The date range is inclusive on both ends. "
        f"day_offset must be an integer from 0 through {max_offset} "
        f"(0 = {from_date.isoformat()}, {max_offset} = {to_date.isoformat()})."
    )
    if target_count is not None:
        count_rules = (
            f"Produce exactly {target_count} items. "
            "Prefer one primary post per calendar day across the inclusive range "
            "(unique day_offset values covering the window). "
            "Choose platforms using the cadence weights (higher posts/week → more items)."
        )
    else:
        count_rules = (
            f"Target about {cadence_target} items for this inclusive "
            f"{inclusive_days}-day window from weekly cadence "
            f"({weekly_total} posts/week). "
            f"Spread day_offset across 0..{max_offset}."
        )
    industry = brand.get("industry") or ""
    pillars = brand.get("pillars") or []
    offer_bits = []
    for pillar in pillars:
        desc = (pillar.get("description") or "").strip()
        name = (pillar.get("name") or pillar.get("title") or "").strip()
        if desc:
            offer_bits.append(desc)
        elif name:
            offer_bits.append(name)
    offer = "; ".join(offer_bits) if offer_bits else "(not provided — invent carefully from industry)"
    user = (
        f"Brand: {brand.get('name')} ({brand.get('city')})\n"
        f"Industry: {industry or '(unknown)'}\n"
        f"Product / service (use this as the core of every post): {offer}\n"
        f"Primary language: {primary_lang}\n"
        f"Range (inclusive): {from_date.isoformat()} to {to_date.isoformat()} "
        f"({inclusive_days} days; day_offset 0..{max_offset})\n"
        f"{count_rules}\n"
        f"Cadence:\n{format_cadence(cadence)}\nOnly gaps: {only_gaps}\n"
        f"Pillars:\n{format_pillars(brand.get('pillars') or [])}\n"
        f"Events:\n{format_cultural_events(cultural_events)}\n"
        f"Existing posts: {existing_summary}\n"
        f"Guidelines:\n{format_guidelines(guidelines)}"
    )
    return [
        LLMMessage(role="system", content=system),
        LLMMessage(role="user", content=user),
    ]
