"""Unit tests for merge and proposal mapping."""

from __future__ import annotations

from app.features.brand_research.merge import (
    field_confidence_map,
    merge,
    to_proposal_patch,
)
from app.features.brand_research.ports import ExtractionResult, FieldExtraction


def _field(
    value: object, confidence: float | None, urls: list[str] | None = None
) -> FieldExtraction:
    return FieldExtraction(value=value, confidence=confidence, source_page_urls=urls or [])


def test_merge_fetch_wins_when_both_present() -> None:
    fetch = ExtractionResult(
        voice_adjectives=_field(["Warm"], 0.9, ["https://a.sa/"]),
        colors=_field(["#111"], 0.95, ["https://a.sa/"]),
        sources=["https://a.sa/"],
    )
    search = ExtractionResult(
        voice_adjectives=_field(["Cold"], 0.7, ["https://review.sa/"]),
        colors=_field(["#222"], 0.6, ["https://review.sa/"]),
        sources=["https://review.sa/"],
    )
    merged = merge(fetch, search)
    assert merged.voice_adjectives.value == ["Warm"]
    assert merged.colors.value == ["#111"]
    assert merged.sources == ["https://a.sa/", "https://review.sa/"]


def test_merge_search_used_when_fetch_empty() -> None:
    fetch = ExtractionResult(warnings=["robots.txt disallows /about"])
    search = ExtractionResult(
        dialect=_field("gulf", 0.5, ["https://twitter.com/x"]),
        sources=["https://twitter.com/x"],
        warnings=["search found limited signal"],
    )
    merged = merge(fetch, search)
    assert merged.dialect.value == "gulf"
    assert merged.warnings == [
        "robots.txt disallows /about",
        "search found limited signal",
    ]


def test_merge_neither_omitted_from_patch() -> None:
    merged = merge(ExtractionResult(), ExtractionResult())
    patch = to_proposal_patch(merged)
    assert patch == {}
    assert field_confidence_map(merged) == {}


def test_proposal_patch_omits_null_confidence() -> None:
    result = ExtractionResult(
        voice_adjectives=_field(["Direct"], 0.8, ["https://a.sa/"]),
        dialect=_field(None, None),
        languages=_field([], None),
        colors=_field(["#0B5D3B"], 0.9, ["https://a.sa/"]),
        pillars_suggested=_field(
            [{"name": "Offers", "description": "Seasonal"}],
            0.4,
            ["https://a.sa/"],
        ),
        sources=["https://a.sa/"],
    )
    patch = to_proposal_patch(result)
    assert patch == {
        "guidelines": {
            "voiceAdjectives": ["Direct"],
            "colors": ["#0B5D3B"],
        },
        "pillars": [{"name": "Offers", "description": "Seasonal"}],
    }
    assert "dialect" not in patch.get("guidelines", {})
    assert "languages" not in patch.get("guidelines", {})
    assert "competitors" not in patch
