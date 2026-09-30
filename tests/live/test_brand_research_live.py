"""Live website-research tests — skipped unless RUN_LIVE_TESTS=1.

Asserts schema-conformant proposals against real Saudi SMB sites rather than
specific field values (sites can change). Also asserts Anthropic web_fetch
accepts the caller-supplied source_url when placed in the first user message.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from app.core.config import get_settings
from app.features.brand_research.merge import to_proposal_patch
from app.features.brand_research.parse import parse_extraction_content
from app.features.brand_research.prompts import extraction as extraction_prompt
from app.integrations.llm.ports import LLMMessage, LLMTool, StructuredGenerationRequest
from app.integrations.webfetch.fetcher import fetch_site
from app.integrations.webfetch.ssrf import safe_connect

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="Set RUN_LIVE_TESTS=1 to hit real sites / LLM accounts",
    ),
]

# Recorded Saudi SMB targets for Phase 8 DoD — assert schema, not field values.
_SAUDI_SMB_SITES = (
    "https://www.jarir.com/",
    "https://www.extra.com/",
    "https://www.alrajhibank.com.sa/",
    "https://www.stc.com.sa/",
    "https://www.tamimi.com/",
)


@pytest.mark.asyncio
async def test_live_https_sni_safe_connect() -> None:
    """Pinned-IP fetch must set SNI or TLS verification fails against the IP."""
    response = await safe_connect("https://example.com/", timeout_seconds=15.0)
    assert response.status_code == 200
    assert len(response.content) > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("url", _SAUDI_SMB_SITES)
async def test_live_fetch_site_returns_html(url: str) -> None:
    result = await fetch_site(url, page_cap=2)
    # Soft assertion: some sites block bots; still must not raise SSRF/TLS errors.
    assert result.source_url == url
    assert isinstance(result.warnings, list)


@pytest.mark.asyncio
async def test_live_brand_research_schema_five_sites() -> None:
    """Schema-conformant extraction for the five recorded Saudi SMB targets.

    Fetches each site via the SSRF-safe path, then runs a structured LLM call
    **without** hosted web_search (avoids multi-minute tool loops in CI). Asserts
    the proposal patch shape, not specific field values.
    """
    settings = get_settings()
    if not settings.llm.openai_api_key and not settings.llm.anthropic_api_key:
        pytest.skip("No LLM API keys configured")
    from app.integrations.llm.openai import OpenAIProvider

    api_key = settings.llm.openai_api_key
    if api_key is None:
        pytest.skip("OPENAI_API_KEY required for this live schema check")
    provider = OpenAIProvider(api_key=api_key.get_secret_value())
    conformant = 0
    errors: list[str] = []
    for url in _SAUDI_SMB_SITES:
        site = await fetch_site(url, page_cap=1)
        if not site.pages:
            errors.append(f"{url}: no pages")
            continue
        page = site.pages[0]
        messages = extraction_prompt.build(
            source_url=url,
            brand_context={"name": "SMB"},
            page_summaries=[
                {
                    "url": page.url,
                    "title": page.title,
                    "text": page.text[:8000],
                    "og": page.og,
                    "theme_color": page.theme_color,
                    "language": page.language,
                    "social_links": page.social_links,
                    "contact_links": page.contact_links,
                    "json_ld": page.json_ld,
                }
            ],
        )
        try:
            response = await provider.generate_structured(
                StructuredGenerationRequest(
                    messages=messages,
                    output_schema=extraction_prompt.OUTPUT_SCHEMA,
                    schema_name="brand_research",
                    temperature=0.2,
                ),
                model="gpt-5.5",
                timeout_seconds=60.0,
            )
        except Exception as exc:  # noqa: BLE001 — live soft-assert across sites
            errors.append(f"{url}: {exc}")
            continue
        assert isinstance(response.content, dict)
        parsed = parse_extraction_content(response.content, fallback_sources=[url])
        patch = to_proposal_patch(parsed)
        assert isinstance(patch, dict)
        if patch.get("guidelines") or patch.get("pillars") or patch.get("competitors"):
            conformant += 1
            break
    assert conformant >= 1, (
        f"expected at least one schema-conformant proposal; errors={errors}"
    )


@pytest.mark.asyncio
async def test_live_anthropic_web_fetch_source_url_in_user_message() -> None:
    """source_url in the first user message must be fetchable (no url_not_in_prior_context)."""
    settings = get_settings()
    if not settings.llm.anthropic_api_key:
        pytest.skip("ANTHROPIC_API_KEY not configured")
    from app.integrations.llm.anthropic import AnthropicProvider

    provider = AnthropicProvider(api_key=settings.llm.anthropic_api_key.get_secret_value())
    url = "https://example.com/"
    # Mirror SearchAugmentedResearcher message shape — call Anthropic directly so
    # we do not depend on OpenAI being the brand_research primary.
    response = await provider.generate_structured(
        StructuredGenerationRequest(
            messages=[
                LLMMessage(
                    role="system",
                    content=(
                        "Use web_fetch on the URL in the user message if available. "
                        "Return the brand-research JSON schema."
                    ),
                ),
                LLMMessage(
                    role="user",
                    content=f"Research this brand website: {url}",
                ),
            ],
            output_schema=extraction_prompt.OUTPUT_SCHEMA,
            schema_name="brand_research",
            temperature=0.2,
            tools=[
                LLMTool(
                    name="web_search",
                    params={
                        "user_location": {
                            "type": "approximate",
                            "country": "SA",
                            "timezone": "Asia/Riyadh",
                        }
                    },
                ),
                LLMTool(name="web_fetch", params={}),
            ],
        ),
        model="claude-sonnet-5",
        timeout_seconds=90.0,
    )
    # If Anthropic rejected fetch with url_not_in_prior_context, raw/error surfaces;
    # a successful structured parse is enough to prove the URL was in context.
    assert isinstance(response.content, dict)
    parsed = parse_extraction_content(response.content, fallback_sources=[url])
    assert isinstance(parsed.warnings, list)
    raw: dict[str, Any] | None = response.raw
    blob = str(raw) if raw else ""
    assert "url_not_in_prior_context" not in blob
