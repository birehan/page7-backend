"""Unit tests for FastExtractResearcher."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import get_settings
from app.features.brand_research.fast_extract import FastExtractResearcher, _run_fast_pipeline
from app.features.brand_research.ports import ResearchRequest
from app.integrations.llm.fakes import FakeLLMProvider
from app.integrations.llm.router import LLMTaskRouter

_SAMPLE_HTML = """\
<!DOCTYPE html>
<html lang="ar">
<head>
  <meta name="theme-color" content="#0B5D3B">
  <title>Example Clinic Riyadh</title>
  <link rel="icon" href="/logo.svg" type="image/svg+xml">
</head>
<body>
  <img class="site-logo" src="/assets/logo.png" width="160" alt="Example Clinic logo">
  <p>Friendly modern dental care and hospital clinic medical services in Riyadh.</p>
  <a href="https://instagram.com/exampleclinic">Instagram</a>
</body>
</html>
"""


def _fake_router() -> LLMTaskRouter:
    fake = FakeLLMProvider()
    return LLMTaskRouter(
        providers={"openai": fake, "anthropic": fake},
        settings=get_settings().llm,
    )


@pytest.mark.asyncio
async def test_fast_pipeline_from_html_file(tmp_path: Path) -> None:
    html_path = tmp_path / "home.html"
    html_path.write_text(_SAMPLE_HTML, encoding="utf-8")
    outcome = await _run_fast_pipeline(
        "https://example-clinic.sa/",
        brand_name="Example Clinic",
        llm=_fake_router(),
        html_file=str(html_path),
    )
    assert outcome.crawled_pages == 1
    assert outcome.content_hash
    assert outcome.result.voice_adjectives.confidence is not None
    assert outcome.result.colors.confidence is not None
    assert outcome.result.colors.value  # HTML theme-color / logo path signals
    assert outcome.result.logo_url.confidence is not None
    assert outcome.result.banned_claims.confidence is not None  # industry fallback


@pytest.mark.asyncio
async def test_fast_researcher_fetch_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _none(_url: str, *, timeout_seconds: float = 10.0) -> None:
        return None

    monkeypatch.setattr(
        "app.features.brand_research.fast_extract._safe_get",
        _none,
    )
    researcher = FastExtractResearcher(_fake_router())
    result = await researcher.research(
        ResearchRequest(source_url="https://example-clinic.sa/", brand_context={})
    )
    assert result.voice_adjectives.confidence is None
    assert any("fetch failed" in w for w in result.warnings)
    assert researcher.last_crawled_pages == 0


@pytest.mark.asyncio
async def test_fast_researcher_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_get(
        url: str, *, timeout_seconds: float = 10.0
    ) -> dict[str, object] | None:
        if "/about" in url:
            return None
        return {"url": url.rstrip("/") + "/", "html": _SAMPLE_HTML, "status": 200}

    monkeypatch.setattr(
        "app.features.brand_research.fast_extract._safe_get",
        _fake_get,
    )
    researcher = FastExtractResearcher(_fake_router())
    result = await researcher.research(
        ResearchRequest(
            source_url="https://example-clinic.sa/",
            brand_context={"name": "Example Clinic"},
        )
    )
    assert result.voice_adjectives.confidence is not None
    assert result.languages.confidence is not None
    assert "ar" in (result.languages.value or [])
    assert researcher.last_content_hash
    assert researcher.last_crawled_pages >= 1
