#!/usr/bin/env python3
"""Approach 01: new fetch-only (crawl + LLM) — preferred CLI / production stage 1.

Wraps ``FetchExtractResearcher`` via ``scripts/extract_onboarding_brand.extract_brand``.
Does not run the search-augmented stage. Does not touch DB/jobs.

Needs: ``backend/.env`` LLM keys (``LLM__OPENAI_API_KEY`` / Anthropic), outbound HTTPS.

Usage (from backend/):

    uv run python scripts/brand_approaches/01_new_fetch_only.py https://www.extra.com/
    uv run python scripts/brand_approaches/01_new_fetch_only.py URL --name Extra
"""

from __future__ import annotations

import asyncio
import time

from _common import build_url_parser, emit, ensure_scripts_on_path, normalize_url, status


def main(argv: list[str] | None = None) -> int:
    parser = build_url_parser(
        "new fetch-only brand extract (crawl + LLM)",
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

    status(f"[01_new_fetch_only] {url}")
    started = time.perf_counter()
    result = asyncio.run(extract_brand(url, brand_name=args.name, fetch_only=True))
    warnings = [str(w) for w in (result.get("warnings") or [])]
    warnings.extend(str(w) for w in (result.get("fetch_warnings") or []))
    ok = result.get("status") in {"succeeded", "partial"}
    return emit(
        approach="01_new_fetch_only",
        url=url,
        started=started,
        result=result,
        warnings=warnings,
        ok=ok,
    )


if __name__ == "__main__":
    raise SystemExit(main())
