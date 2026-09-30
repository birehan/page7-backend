"""Minimal PII scrub for crawl markdown (vendored from pgblank for lab use)."""

from __future__ import annotations

import re
from typing import Any

REDACTED_TEXT = "[redacted]"

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+", re.UNICODE)
_SEPARATORS = "[\\s\\-\u2011]"
_SAUDI_MOBILE = re.compile(
    rf"(?:\+?966|00966){_SEPARATORS}?5(?:{_SEPARATORS}|\d){{8,12}}"
    rf"|\b05(?:{_SEPARATORS}|\d){{8,12}}"
)
_E164 = re.compile(rf"\+\d{{1,3}}{_SEPARATORS}?\d{{6,14}}\b")
_PATTERNS = (_EMAIL, _SAUDI_MOBILE, _E164)


def scrub_free_text(text: str) -> str:
    scrubbed = text
    for pattern in _PATTERNS:
        scrubbed = pattern.sub(REDACTED_TEXT, scrubbed)
    return scrubbed


def scrub_free_text_values(value: object) -> object:
    if isinstance(value, str):
        return scrub_free_text(value)
    if isinstance(value, dict):
        return scrub_free_text_mapping(value)
    if isinstance(value, list):
        return [scrub_free_text_values(item) for item in value]
    return value


def scrub_free_text_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return {key: scrub_free_text_values(item) for key, item in value.items()}
