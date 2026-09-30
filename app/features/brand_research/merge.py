"""Field-level merge: fetch wins when present (architecture/08 §2.3)."""

from __future__ import annotations

from typing import Any

from app.features.brand_research.ports import (
    EXTRACTION_FIELD_NAMES,
    FIELD_TO_PATCH_KEY,
    GUIDELINE_FIELDS,
    ExtractionResult,
    FieldExtraction,
)


def merge(fetch: ExtractionResult, search: ExtractionResult) -> ExtractionResult:
    """Pick one whole field value — never average or blend lists."""
    merged: dict[str, FieldExtraction] = {}
    for name in EXTRACTION_FIELD_NAMES:
        fetch_field: FieldExtraction = getattr(fetch, name)
        search_field: FieldExtraction = getattr(search, name)
        if fetch_field.confidence is not None:
            merged[name] = fetch_field
        elif search_field.confidence is not None:
            merged[name] = search_field
        else:
            merged[name] = FieldExtraction()
    sources = _dedupe([*fetch.sources, *search.sources])
    warnings = [*fetch.warnings, *search.warnings]
    return ExtractionResult(**merged, sources=sources, warnings=warnings)


def to_proposal_patch(result: ExtractionResult) -> dict[str, Any]:
    """Map ExtractionResult to the frontend Apply patch; omit unset fields."""
    guidelines: dict[str, Any] = {}
    patch: dict[str, Any] = {}
    for name in EXTRACTION_FIELD_NAMES:
        field: FieldExtraction = getattr(result, name)
        if field.confidence is None or field.value is None:
            continue
        key = FIELD_TO_PATCH_KEY[name]
        if name in GUIDELINE_FIELDS:
            guidelines[key] = field.value
        else:
            patch[key] = field.value
    if guidelines:
        patch["guidelines"] = guidelines
    return patch


def field_confidence_map(result: ExtractionResult) -> dict[str, float]:
    out: dict[str, float] = {}
    for name in EXTRACTION_FIELD_NAMES:
        field: FieldExtraction = getattr(result, name)
        if field.confidence is None:
            continue
        out[FIELD_TO_PATCH_KEY[name]] = field.confidence
    return out


def field_sources_map(result: ExtractionResult) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for name in EXTRACTION_FIELD_NAMES:
        field: FieldExtraction = getattr(result, name)
        if field.confidence is None:
            continue
        out[FIELD_TO_PATCH_KEY[name]] = list(field.source_page_urls)
    return out


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out
