"""Parse structured LLM extraction output into ExtractionResult."""

from __future__ import annotations

import re
from typing import Any

from app.features.brand_research.ports import (
    EXTRACTION_FIELD_NAMES,
    ExtractionResult,
    FieldExtraction,
)

_VALID_LANGS = frozenset({"ar", "en"})
_HEX_RE = re.compile(r"^#([0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})$")
_MAX_COLORS = 3


def parse_extraction_content(
    content: dict[str, Any],
    *,
    fallback_sources: list[str] | None = None,
    extra_warnings: list[str] | None = None,
) -> ExtractionResult:
    fields: dict[str, FieldExtraction] = {}
    warnings: list[str] = list(extra_warnings or [])
    for name in EXTRACTION_FIELD_NAMES:
        raw = content.get(name)
        field, drop_warning = _parse_field(name, raw)
        fields[name] = field
        if drop_warning:
            warnings.append(drop_warning)
    raw_warnings = content.get("warnings")
    if isinstance(raw_warnings, list):
        warnings.extend(str(w) for w in raw_warnings)
    sources = list(fallback_sources or [])
    for field in fields.values():
        sources.extend(field.source_page_urls)
    return ExtractionResult(
        **fields,
        sources=_dedupe(sources),
        warnings=warnings,
    )


def _parse_field(name: str, raw: object) -> tuple[FieldExtraction, str | None]:
    if not isinstance(raw, dict):
        return FieldExtraction(), None
    confidence = raw.get("confidence")
    if confidence is not None:
        try:
            confidence_f = float(confidence)
        except (TypeError, ValueError):
            return FieldExtraction(), f"dropped {name}: invalid confidence"
        if not 0.0 <= confidence_f <= 1.0:
            return FieldExtraction(), f"dropped {name}: confidence out of range"
    else:
        confidence_f = None
    value = raw.get("value")
    urls_raw = raw.get("source_page_urls") or []
    urls = [str(u) for u in urls_raw] if isinstance(urls_raw, list) else []
    # Anthropic schema forbids null confidence — models send 0 for "unknown".
    if confidence_f is None or confidence_f == 0.0:
        return FieldExtraction(), None
    if isinstance(value, str) and not value.strip():
        return FieldExtraction(), None
    validated, err = _validate_value(name, value)
    if err:
        return FieldExtraction(), f"dropped {name}: {err}"
    return FieldExtraction(value=validated, confidence=confidence_f, source_page_urls=urls), None


def _validate_value(name: str, value: object) -> tuple[object | None, str | None]:
    if value is None:
        return None, "null value"
    if name == "name":
        if not isinstance(value, str):
            return None, "name must be a string"
        cleaned = value.strip()
        if not cleaned:
            return None, "empty name"
        return cleaned[:80], None
    if name == "industry":
        if not isinstance(value, str):
            return None, "industry must be a string"
        cleaned = value.strip()
        if not cleaned:
            return None, "empty industry"
        return cleaned[:64], None
    if name == "description":
        if not isinstance(value, str):
            return None, "description must be a string"
        cleaned = " ".join(value.split()).strip()
        if not cleaned:
            return None, "empty description"
        return cleaned[:280], None
    if name == "languages":
        preferred = _preferred_language(value)
        if preferred is None:
            return None, "no valid preferred language"
        return [preferred], None
    if name == "colors":
        if not isinstance(value, list):
            return None, "expected list"
        colors: list[str] = []
        for item in value:
            hex_val = _normalize_hex(item)
            if hex_val and hex_val not in colors:
                colors.append(hex_val)
            if len(colors) >= _MAX_COLORS:
                break
        if not colors:
            return None, "empty list"
        return colors, None
    if name == "logo_url":
        if not isinstance(value, str):
            return None, "logo_url must be a string"
        url = value.strip()
        if not url.startswith(("http://", "https://")):
            return None, "logo_url must be an http(s) URL"
        if len(url) > 2048:
            return None, "logo_url too long"
        return url, None
    return value, None


def _preferred_language(value: object) -> str | None:
    """Pick a single preferred language (ar|en). First valid wins for lists."""
    if isinstance(value, str):
        code = value.strip().lower()
        if code in _VALID_LANGS:
            return code
        if code.startswith("ar"):
            return "ar"
        if code.startswith("en"):
            return "en"
        return None
    if isinstance(value, list):
        for item in value:
            preferred = _preferred_language(item)
            if preferred:
                return preferred
    return None


def _normalize_hex(raw: object) -> str | None:
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s.startswith("#"):
        s = f"#{s}"
    if not _HEX_RE.match(s):
        return None
    if len(s) == 4:
        return f"#{s[1]*2}{s[2]*2}{s[3]*2}".upper()
    return s.upper()


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out
