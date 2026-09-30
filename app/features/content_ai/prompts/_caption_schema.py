"""Caption variant output schema shared by generation and transform prompts."""

from __future__ import annotations

from typing import Any

_HASHTAGS_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "string"},
}

_LANG_VARIANT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "caption": {"type": "string"},
        "hashtags": _HASHTAGS_SCHEMA,
    },
    "required": ["caption", "hashtags"],
    "additionalProperties": False,
}

CAPTION_VARIANT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ar": _LANG_VARIANT_SCHEMA,
        "en": _LANG_VARIANT_SCHEMA,
        "first_comment": {"type": ["string", "null"]},
    },
    "required": ["ar", "en", "first_comment"],
    "additionalProperties": False,
}

CAPTION_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "variants": {
            "type": "array",
            "items": CAPTION_VARIANT_SCHEMA,
        },
    },
    "required": ["variants"],
    "additionalProperties": False,
}
