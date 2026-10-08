"""Shared prompt context formatting for content AI modules."""

from __future__ import annotations

import json
from typing import Any


def format_guidelines(guidelines: dict[str, Any]) -> str:
    if not guidelines:
        return "(none provided)"
    return json.dumps(guidelines, ensure_ascii=False, indent=2)


def format_banned_claims(banned_claims: list[str]) -> str:
    if not banned_claims:
        return "(none)"
    return "\n".join(f"- {claim}" for claim in banned_claims)


def format_pillar(pillar: dict[str, Any] | None) -> str:
    if pillar is None:
        return "(none — general brand post)"
    name = pillar.get("name") or pillar.get("title") or "(unnamed pillar)"
    description = pillar.get("description") or ""
    lines = [f"Name: {name}"]
    if description:
        lines.append(f"Description: {description}")
    return "\n".join(lines)


def format_cultural_event(event: dict[str, Any] | None) -> str:
    if event is None:
        return "(none)"
    name = event.get("name") or event.get("title") or "(unnamed event)"
    description = event.get("description") or ""
    lines = [f"Name: {name}"]
    if description:
        lines.append(f"Description: {description}")
    return "\n".join(lines)


def format_platforms(platforms: list[str]) -> str:
    return ", ".join(platforms) if platforms else "(none)"


def format_pillars(pillars: list[dict[str, Any]]) -> str:
    if not pillars:
        return "(none)"
    lines: list[str] = []
    for pillar in pillars:
        pillar_id = pillar.get("id", "")
        name = pillar.get("name") or pillar.get("title") or "(unnamed)"
        description = (pillar.get("description") or "").strip()
        if description:
            lines.append(f"- id={pillar_id}: {name} — {description}")
        else:
            lines.append(f"- id={pillar_id}: {name}")
    return "\n".join(lines)


def format_cultural_events(events: list[dict[str, Any]]) -> str:
    if not events:
        return "(none)"
    lines: list[str] = []
    for event in events:
        event_id = event.get("id", "")
        name = event.get("name") or event.get("title") or "(unnamed)"
        lines.append(f"- id={event_id}: {name}")
    return "\n".join(lines)


def format_cadence(cadence: dict[str, int]) -> str:
    if not cadence:
        return "(none)"
    return "\n".join(f"- {platform}: {count} posts/week" for platform, count in cadence.items())


def format_goals(goals: list[dict[str, Any]]) -> str:
    if not goals:
        return "(none)"
    lines: list[str] = []
    for goal in goals:
        title = goal.get("title") or goal.get("text") or "(untitled)"
        description = goal.get("description") or ""
        if description:
            lines.append(f"- {title}: {description}")
        else:
            lines.append(f"- {title}")
    return "\n".join(lines)
