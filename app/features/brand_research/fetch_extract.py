"""FetchExtractResearcher — first-party site fetch + LLM extraction."""

from __future__ import annotations

from typing import Any

from app.features.brand_research.parse import parse_extraction_content
from app.features.brand_research.ports import (
    EXTRACTION_FIELD_NAMES,
    ExtractionResult,
    FieldExtraction,
    ResearchRequest,
)
from app.features.brand_research.prompts import extraction as extraction_prompt
from app.integrations.errors import ProviderUnavailableError
from app.integrations.llm.ports import StructuredGenerationRequest
from app.integrations.llm.router import LLMTaskRouter
from app.integrations.webfetch.extract import PageExtraction
from app.integrations.webfetch.fetcher import SiteFetchResult, fetch_site


class FetchExtractResearcher:
    """Implements ResearchProvider over integrations/webfetch + brand_research LLM."""

    def __init__(self, llm_router: LLMTaskRouter) -> None:
        self._llm = llm_router
        self.last_site: SiteFetchResult | None = None

    async def research(self, request: ResearchRequest) -> ExtractionResult:
        try:
            site = await fetch_site(request.source_url)
        except ProviderUnavailableError as exc:
            return ExtractionResult(warnings=[f"fetch failed: {exc}"])
        self.last_site = site

        if not site.pages:
            return ExtractionResult(
                sources=[],
                warnings=list(site.warnings) or ["no pages fetched"],
            )

        structured = _structured_signals(site.pages)
        page_summaries = [_page_summary(page) for page in site.pages]
        messages = extraction_prompt.build(
            source_url=request.source_url,
            brand_context=request.brand_context,
            page_summaries=page_summaries,
        )
        response = await self._llm.run_structured(
            "brand_research",
            StructuredGenerationRequest(
                messages=messages,
                output_schema=extraction_prompt.OUTPUT_SCHEMA,
                schema_name="brand_research",
                temperature=0.2,
            ),
        )
        llm_result = parse_extraction_content(
            response.content,
            fallback_sources=[page.url for page in site.pages],
            extra_warnings=list(site.warnings),
        )
        return _overlay_structured(llm_result, structured)


def _page_summary(page: PageExtraction) -> dict[str, Any]:
    return {
        "url": page.url,
        "title": page.title,
        "text": page.text,
        "og": page.og,
        "theme_color": page.theme_color,
        "language": page.language,
        "social_links": page.social_links,
        "contact_links": page.contact_links,
        "json_ld": page.json_ld,
        "logo_url": page.logo_url,
    }


def _structured_signals(pages: list[PageExtraction]) -> dict[str, FieldExtraction]:
    """High-confidence signals from machine-authored tags (architecture/08 §8)."""
    out: dict[str, FieldExtraction] = {}
    colors: list[str] = []
    color_urls: list[str] = []
    languages: list[str] = []
    lang_urls: list[str] = []
    logo_url: str | None = None
    logo_page_url: str | None = None
    for page in pages:
        if page.theme_color and page.theme_color not in colors:
            colors.append(page.theme_color)
            color_urls.append(page.url)
        if page.language:
            lang = page.language.split("-")[0].lower()
            if lang in {"ar", "en"} and lang not in languages:
                languages.append(lang)
                lang_urls.append(page.url)
        if logo_url is None and page.logo_url:
            logo_url = page.logo_url
            logo_page_url = page.url
    if colors:
        out["colors"] = FieldExtraction(
            value=colors[:3], confidence=0.95, source_page_urls=color_urls[:3]
        )
    if languages:
        # Preferred language: first detected ar|en only
        preferred = languages[0]
        out["languages"] = FieldExtraction(
            value=[preferred], confidence=0.9, source_page_urls=lang_urls[:1]
        )
    if logo_url is not None and logo_page_url is not None:
        out["logo_url"] = FieldExtraction(
            value=logo_url, confidence=0.9, source_page_urls=[logo_page_url]
        )
    return out


def _overlay_structured(
    llm: ExtractionResult, structured: dict[str, FieldExtraction]
) -> ExtractionResult:
    """Structured tags win over LLM inference for the same field."""
    data = llm.model_dump()
    for name, field in structured.items():
        data[name] = field.model_dump()
    sources = list(llm.sources)
    for field in structured.values():
        sources.extend(field.source_page_urls)
    fields = {
        name: FieldExtraction(**data[name])
        for name in EXTRACTION_FIELD_NAMES
    }
    deduped: list[str] = []
    seen: set[str] = set()
    for url in sources:
        if url not in seen:
            seen.add(url)
            deduped.append(url)
    return ExtractionResult(**fields, sources=deduped, warnings=list(llm.warnings))
