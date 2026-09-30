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
            voice_adjectives=FieldExtraction(
                value=["Friendly", "Modern", "Trustworthy"],
                confidence=0.8,
                source_page_urls=["https://example.sa/"],
            ),
            colors=FieldExtraction(
                value=["#0B5D3B"],
                confidence=0.9,
                source_page_urls=["https://example.sa/"],
            ),
            sources=["https://example.sa/"],
            warnings=[],
        )

    async def research(self, request: ResearchRequest) -> ExtractionResult:
        self.calls.append(request)
        return self._result
