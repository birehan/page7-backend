"""Search stage must degrade on timeout so fetch results are not discarded."""

from __future__ import annotations

import asyncio

import pytest

from app.features.brand_research.ports import ExtractionResult, ResearchRequest
from app.features.brand_research.tasks import _research_with_timeout


class _HungSearchResearcher:
    async def research(self, request: ResearchRequest) -> ExtractionResult:
        await asyncio.sleep(3600)
        return ExtractionResult()


@pytest.mark.asyncio
async def test_search_timeout_returns_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.features.brand_research.tasks._SEARCH_STAGE_TIMEOUT_SECONDS",
        0.05,
    )
    result = await _research_with_timeout(
        _HungSearchResearcher(),  # type: ignore[arg-type]
        ResearchRequest(source_url="https://example.com", brand_context={}),
    )
    assert result.name.confidence is None
    assert any("timed out" in w for w in result.warnings)
