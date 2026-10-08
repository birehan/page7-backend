"""Unit tests for merge and proposal mapping (identity-only)."""

from __future__ import annotations

from app.features.brand_research.merge import (
    field_confidence_map,
    merge,
    to_proposal_patch,
)
from app.features.brand_research.ports import (
    IDENTITY_GUIDELINE_KEYS,
    IDENTITY_PATCH_KEYS,
    ExtractionResult,
    FieldExtraction,
)


def _field(
    value: object, confidence: float | None, urls: list[str] | None = None
) -> FieldExtraction:
    return FieldExtraction(value=value, confidence=confidence, source_page_urls=urls or [])


def test_merge_fetch_wins_when_both_present() -> None:
    fetch = ExtractionResult(
        name=_field("Warm Co", 0.9, ["https://a.sa/"]),
        colors=_field(["#111111"], 0.95, ["https://a.sa/"]),
        sources=["https://a.sa/"],
    )
    search = ExtractionResult(
        name=_field("Cold Co", 0.7, ["https://review.sa/"]),
        colors=_field(["#222222"], 0.6, ["https://review.sa/"]),
        sources=["https://review.sa/"],
    )
    merged = merge(fetch, search)
    assert merged.name.value == "Warm Co"
    assert merged.colors.value == ["#111111"]
    assert merged.sources == ["https://a.sa/", "https://review.sa/"]


def test_merge_search_used_when_fetch_empty() -> None:
    fetch = ExtractionResult(warnings=["robots.txt disallows /about"])
    search = ExtractionResult(
        languages=_field(["ar"], 0.5, ["https://twitter.com/x"]),
        sources=["https://twitter.com/x"],
        warnings=["search found limited signal"],
    )
    merged = merge(fetch, search)
    assert merged.languages.value == ["ar"]
    assert merged.warnings == [
        "robots.txt disallows /about",
        "search found limited signal",
    ]


def test_merge_neither_omitted_from_patch() -> None:
    merged = merge(ExtractionResult(), ExtractionResult())
    patch = to_proposal_patch(merged)
    assert patch == {}
    assert field_confidence_map(merged) == {}


def test_proposal_patch_identity_only() -> None:
    result = ExtractionResult(
        name=_field("Direct Clinic", 0.8, ["https://a.sa/"]),
        industry=_field("healthcare", 0.7, ["https://a.sa/"]),
        description=_field("Family dental care.", 0.75, ["https://a.sa/"]),
        languages=_field(["ar"], 0.9, ["https://a.sa/"]),
        colors=_field(["#0B5D3B", "#134E4A", "#F5F5F4"], 0.9, ["https://a.sa/"]),
        logo_url=_field("https://a.sa/logo.png", 0.85, ["https://a.sa/"]),
        sources=["https://a.sa/"],
    )
    patch = to_proposal_patch(result)
    assert set(patch.keys()) <= IDENTITY_PATCH_KEYS
    assert set(patch["guidelines"].keys()) <= IDENTITY_GUIDELINE_KEYS
    assert patch == {
        "name": "Direct Clinic",
        "industry": "healthcare",
        "description": "Family dental care.",
        "logoUrl": "https://a.sa/logo.png",
        "guidelines": {
            "colors": ["#0B5D3B", "#134E4A", "#F5F5F4"],
            "languages": ["ar"],
        },
    }
    assert "voiceAdjectives" not in patch.get("guidelines", {})
    assert "dialect" not in patch.get("guidelines", {})
    assert "pillars" not in patch
    assert "competitors" not in patch
    assert len(patch["guidelines"]["colors"]) <= 3
    assert patch["guidelines"]["languages"] == ["ar"]
