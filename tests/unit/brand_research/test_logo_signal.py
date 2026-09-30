"""Logo structured signal → proposal patch."""

from __future__ import annotations

from app.features.brand_research.fetch_extract import _overlay_structured, _structured_signals
from app.features.brand_research.merge import to_proposal_patch
from app.features.brand_research.ports import ExtractionResult, FieldExtraction
from app.integrations.webfetch.extract import PageExtraction


def test_structured_signals_emits_logo_url_first_page_wins() -> None:
    pages = [
        PageExtraction(
            url="https://a.sa/",
            title="A",
            text="hi",
            og={},
            json_ld=[],
            social_links=[],
            contact_links=[],
            logo_url="https://a.sa/logo.png",
            theme_color="#111",
            language="ar",
            same_site_links=[],
        ),
        PageExtraction(
            url="https://a.sa/about",
            title="About",
            text="about",
            og={},
            json_ld=[],
            social_links=[],
            contact_links=[],
            logo_url="https://a.sa/other.png",
            theme_color=None,
            language=None,
            same_site_links=[],
        ),
    ]
    signals = _structured_signals(pages)
    assert signals["logo_url"].value == "https://a.sa/logo.png"
    assert signals["logo_url"].confidence == 0.9


def test_overlay_and_patch_include_top_level_logo_url() -> None:
    llm = ExtractionResult()
    structured = {
        "logo_url": FieldExtraction(
            value="https://cdn.sa/logo.png",
            confidence=0.9,
            source_page_urls=["https://cdn.sa/"],
        )
    }
    merged = _overlay_structured(llm, structured)
    patch = to_proposal_patch(merged)
    assert patch["logoUrl"] == "https://cdn.sa/logo.png"
    assert "guidelines" not in patch or "logoUrl" not in patch.get("guidelines", {})
