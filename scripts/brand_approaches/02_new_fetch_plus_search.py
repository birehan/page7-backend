#!/usr/bin/env python3
"""Approach 02: new full production pipeline (fetch + search-augmented LLM).

Mirrors ``run_research_pipeline`` stages without DB/jobs: fetch-extract, then
search-augmented (90s timeout, degrade on failure), then merge → proposal patch.

Needs: ``backend/.env`` LLM keys, outbound HTTPS + LLM web search/fetch tools.

Usage (from backend/):

    uv run python scripts/brand_approaches/02_new_fetch_plus_search.py https://www.extra.com/
    uv run python scripts/brand_approaches/02_new_fetch_plus_search.py URL --name Extra
"""

from __future__ import annotations

import asyncio
import time

from _common import build_url_parser, emit, ensure_scripts_on_path, normalize_url, status


def main(argv: list[str] | None = None) -> int:
    parser = build_url_parser(
        "new full brand extract (fetch + search-augmented LLM)",
        extra=lambda p: p.add_argument(
            "--name",
            default=None,
            help="Optional brand name hint",
        ),
    )
    args = parser.parse_args(argv)
    url = normalize_url(args.url)

    ensure_scripts_on_path()
    from extract_onboarding_brand import extract_brand

    status(f"[02_new_fetch_plus_search] {url}")
    started = time.perf_counter()
    result = asyncio.run(extract_brand(url, brand_name=args.name, fetch_only=False))
    warnings = [str(w) for w in (result.get("warnings") or [])]
    warnings.extend(str(w) for w in (result.get("fetch_warnings") or []))
    warnings.extend(str(w) for w in (result.get("search_warnings") or []))
    ok = result.get("status") in {"succeeded", "partial"}
    return emit(
        approach="02_new_fetch_plus_search",
        url=url,
        started=started,
        result=result,
        warnings=warnings,
        ok=ok,
    )


if __name__ == "__main__":
    raise SystemExit(main())
