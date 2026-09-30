"""Parse structured LLM extraction output into ExtractionResult."""

from __future__ import annotations

from typing import Any

from app.features.brand_research.ports import (
    EXTRACTION_FIELD_NAMES,
    ExtractionResult,
    FieldExtraction,
)

_VALID_DIALECTS = frozenset({"gulf", "msa"})
_VALID_LANGS = frozenset({"ar", "en"})


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
    if name == "dialect":
        if not isinstance(value, str) or value not in _VALID_DIALECTS:
            return None, "dialect must be gulf or msa"
        return value, None
    if name == "languages":
        if not isinstance(value, list):
            return None, "languages must be a list"
        langs = [str(v) for v in value if str(v) in _VALID_LANGS]
        if not langs:
            return None, "no valid languages"
        return langs, None
    if name in {
        "voice_adjectives",
        "do_list",
        "dont_list",
        "banned_claims",
        "colors",
    }:
        if not isinstance(value, list):
            return None, "expected list"
        items = [str(v).strip() for v in value if str(v).strip()]
        if not items:
            return None, "empty list"
        return items, None
    if name == "pillars_suggested":
        if not isinstance(value, list):
            return None, "expected list"
        pillars: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            n = item.get("name")
            d = item.get("description")
            if isinstance(n, str) and isinstance(d, str) and n.strip():
                pillars.append({"name": n.strip(), "description": d.strip()})
        if not pillars:
            return None, "no valid pillars"
        return pillars, None
    if name == "competitors_suggested":
        if not isinstance(value, list):
            return None, "expected list"
        comps: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            handle = item.get("handle")
            platform = item.get("platform")
            if isinstance(handle, str) and isinstance(platform, str) and handle.strip():
                comps.append({"handle": handle.strip(), "platform": platform.strip()})
        if not comps:
            return None, "no valid competitors"
        return comps, None
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


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out
