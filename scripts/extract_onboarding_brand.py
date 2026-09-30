#!/usr/bin/env python3
"""CLI: extract onboarding brand fields from a website URL.

Default approach (from 5-site comparison): **fetch-only** — website crawl + LLM.
Pass ``--with-search`` to also run the search-augmented stage (slower; rarely adds
fields beyond competitors).

Does not touch the database or job queue — only the research stages.

Usage (from backend/):

    uv run python scripts/extract_onboarding_brand.py https://www.extra.com/
    uv run python scripts/extract_onboarding_brand.py URL --with-search
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any
from urllib.parse import urlparse

from app.core.config import get_settings
from app.features.brand_research.fetch_extract import FetchExtractResearcher
from app.features.brand_research.merge import (
    field_confidence_map,
    merge,
    to_proposal_patch,
)
from app.features.brand_research.ports import (
    EXTRACTION_FIELD_NAMES,
    ExtractionResult,
    ResearchRequest,
)
from app.features.brand_research.search_augmented import SearchAugmentedResearcher
from app.integrations.llm import get_llm_router

_SEARCH_STAGE_TIMEOUT_SECONDS = 90.0


def _normalize_url(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise SystemExit("error: empty URL")
    parsed = urlparse(text if "://" in text else f"https://{text}")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit(f"error: invalid URL: {raw!r}")
    return parsed.geturl()


def _has_any_field(result: ExtractionResult) -> bool:
    return any(getattr(result, name).confidence is not None for name in EXTRACTION_FIELD_NAMES)


async def _research_with_timeout(
    researcher: SearchAugmentedResearcher,
    request: ResearchRequest,
) -> ExtractionResult:
    from app.integrations.errors import (
        ProviderRateLimitedError,
        ProviderTimeoutError,
        ProviderUnavailableError,
    )

    try:
        return await asyncio.wait_for(
            researcher.research(request),
            timeout=_SEARCH_STAGE_TIMEOUT_SECONDS,
        )
    except TimeoutError:
        return ExtractionResult(warnings=["search stage timed out"])
    except (ProviderTimeoutError, ProviderUnavailableError, ProviderRateLimitedError) as exc:
        return ExtractionResult(warnings=[f"search failed: {exc}"])


def _is_partial(
    fetch: ExtractionResult, search: ExtractionResult, merged: ExtractionResult
) -> bool:
    fetch_ok = _has_any_field(fetch)
    search_ok = _has_any_field(search)
    if fetch_ok and search_ok and not merged.warnings:
        return False
    if fetch_ok ^ search_ok:
        return True
    return bool(merged.warnings) and _has_any_field(merged)


def _timing_block(
    *,
    started: float,
    fetch_extract_seconds: float,
    search_seconds: float | None,
) -> dict[str, Any]:
    return {
        "elapsed_seconds": round(time.perf_counter() - started, 3),
        "timing": {
            "fetch_extract_seconds": round(fetch_extract_seconds, 3),
            "search_seconds": (None if search_seconds is None else round(search_seconds, 3)),
        },
    }


async def extract_brand(
    source_url: str,
    *,
    brand_name: str | None = None,
    fetch_only: bool = True,
) -> dict[str, Any]:
    """Run research stages; default is fetch-only (comparison winner)."""
    started = time.perf_counter()
    settings = get_settings()
    llm_router = get_llm_router(settings)
    brand_context: dict[str, str] = {}
    if brand_name:
        brand_context["name"] = brand_name

    request = ResearchRequest(source_url=source_url, brand_context=brand_context)
    fetch_researcher = FetchExtractResearcher(llm_router)

    fetch_started = time.perf_counter()
    fetch_result = await fetch_researcher.research(request)
    fetch_extract_seconds = time.perf_counter() - fetch_started
    site = fetch_researcher.last_site

    search_seconds: float | None = None
    if fetch_only:
        merged = fetch_result
        search_result = ExtractionResult(warnings=["search skipped (fetch-only)"])
    else:
        search_researcher = SearchAugmentedResearcher(llm_router)
        search_started = time.perf_counter()
        search_result = await _research_with_timeout(search_researcher, request)
        search_seconds = time.perf_counter() - search_started
        merged = merge(fetch_result, search_result)

    timing = _timing_block(
        started=started,
        fetch_extract_seconds=fetch_extract_seconds,
        search_seconds=search_seconds,
    )

    if not _has_any_field(merged):
        return {
            "status": "failed",
            "error": {
                "code": "RESEARCH_NO_CONTENT",
                "message": "No extractable brand information found",
            },
            "fetch_warnings": list(fetch_result.warnings),
            "search_warnings": list(search_result.warnings),
            "crawled_pages": site.crawled_pages if site else 0,
            "content_hash": site.content_hash if site else None,
            "approach": "fetch_only" if fetch_only else "full",
            **timing,
        }

    status = "partial" if _is_partial(fetch_result, search_result, merged) else "succeeded"
    return {
        "status": status,
        "type": "proposal",
        "patch": to_proposal_patch(merged),
        "confidence": field_confidence_map(merged),
        "sources": list(merged.sources),
        "warnings": list(merged.warnings),
        "crawled_pages": site.crawled_pages if site else 0,
        "content_hash": site.content_hash if site else None,
        "approach": "fetch_only" if fetch_only else "full",
        **timing,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Extract onboarding brand fields from a website. "
            "Default: fetch-only (crawl + LLM). Use --with-search for full pipeline."
        ),
    )
    parser.add_argument("url", help="Brand website URL")
    parser.add_argument("--name", default=None, help="Optional brand name hint")
    parser.add_argument(
        "--with-search",
        action="store_true",
        help="Also run search-augmented stage (slower; up to +90s)",
    )
    parser.add_argument(
        "--fetch-only",
        action="store_true",
        help="Crawl + LLM only (default if --with-search is omitted)",
    )
    args = parser.parse_args(argv)
    source_url = _normalize_url(args.url)
    if args.with_search and args.fetch_only:
        raise SystemExit("error: pass only one of --with-search / --fetch-only")
    fetch_only = not args.with_search

    result = asyncio.run(extract_brand(source_url, brand_name=args.name, fetch_only=fetch_only))
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0 if result.get("status") in {"succeeded", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
