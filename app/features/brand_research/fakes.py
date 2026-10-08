"""FakeResearchProvider — deterministic, zero network (architecture/06 §5)."""

from __future__ import annotations

from app.features.brand_research.ports import (
    ExtractionResult,
    FieldExtraction,
    ResearchRequest,
)


class FakeResearchProvider:
    def __init__(self, *, result: ExtractionResult | None = None) -> None:
        self.calls: list[ResearchRequest] = []
        self._result = result or ExtractionResult(
            name=FieldExtraction(
                value="Example Clinic",
                confidence=0.9,
                source_page_urls=["https://example.sa/"],
            ),
            industry=FieldExtraction(
                value="healthcare",
                confidence=0.7,
                source_page_urls=["https://example.sa/"],
            ),
            description=FieldExtraction(
                value="Family dental care in Riyadh.",
                confidence=0.8,
                source_page_urls=["https://example.sa/"],
            ),
            colors=FieldExtraction(
                value=["#0B5D3B", "#134E4A", "#F5F5F4"],
                confidence=0.9,
                source_page_urls=["https://example.sa/"],
            ),
            languages=FieldExtraction(
                value=["ar"],
                confidence=0.95,
                source_page_urls=["https://example.sa/"],
            ),
            logo_url=FieldExtraction(
                value="https://example.sa/logo.png",
                confidence=0.85,
                source_page_urls=["https://example.sa/"],
            ),
            sources=["https://example.sa/"],
            warnings=[],
        )

    async def research(self, request: ResearchRequest) -> ExtractionResult:
        self.calls.append(request)
        return self._result
