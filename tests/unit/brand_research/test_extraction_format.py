"""Unit tests for brand-research prompt page formatting (brand-research-v2)."""

from __future__ import annotations

from app.features.brand_research.prompts import extraction as extraction_prompt


def test_prompt_version_bumped() -> None:
    assert extraction_prompt.PROMPT_VERSION == "brand-research-v3"


def test_format_pages_includes_social_contact_and_json_ld() -> None:
    blob = extraction_prompt._format_pages(
        [
            {
                "url": "https://clinic.sa/",
                "title": "Noor Dental",
                "language": "ar",
                "theme_color": "#0A7",
                "og": {"og:title": "Noor"},
                "social_links": ["https://instagram.com/noor"],
                "contact_links": ["mailto:hi@clinic.sa"],
                "json_ld": {
                    "@type": "LocalBusiness",
                    "name": "Noor Dental",
                    "address": "Riyadh",
                },
                "text": "Welcome",
            }
        ]
    )
    assert "social_links: ['https://instagram.com/noor']" in blob
    assert "contact_links: ['mailto:hi@clinic.sa']" in blob
    assert "json_ld:" in blob
    assert "LocalBusiness" in blob
    assert "Noor Dental" in blob


def test_format_pages_truncates_oversized_json_ld() -> None:
    huge = {"@type": "Organization", "description": "x" * 5000}
    blob = extraction_prompt._format_pages(
        [
            {
                "url": "https://clinic.sa/",
                "title": "Big",
                "language": "en",
                "theme_color": "",
                "og": {},
                "social_links": [],
                "contact_links": [],
                "json_ld": huge,
                "text": "ok",
            }
        ]
    )
    assert "…(truncated)" in blob
    # Cap is 1500 chars of JSON-LD content plus the marker; ensure we did not dump all 5k.
    assert "x" * 2000 not in blob


def test_format_pages_empty_json_ld_is_none() -> None:
    blob = extraction_prompt._format_pages(
        [
            {
                "url": "https://clinic.sa/",
                "title": "T",
                "text": "hi",
                "og": {},
                "social_links": [],
                "contact_links": [],
                "json_ld": [],
            }
        ]
    )
    assert "json_ld: (none)" in blob
