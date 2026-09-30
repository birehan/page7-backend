#!/usr/bin/env python3
"""Approach 04: pgblank-style pipeline (Firecrawl ∥ dembrandt + optional LLM).

Runs in-process via ``_pgblank/`` (vendored lab helpers). Does **not** subprocess
``/home/babi/pgblank/apps/api`` or copy ``OPENAI_API_KEY``.

Needs:
  - Firecrawl on ``http://127.0.0.1:3002`` (``FIRECRAWL_BASE_URL``)
  - dembrandt + Chromium (optional; missing → empty measured branding)
  - For LLM draft: ``backend/.env`` ``LLM__OPENAI_API_KEY`` / Anthropic via new's router

Usage (from backend/):

    uv run python scripts/brand_approaches/04_pgblank_pipeline.py https://obeidhospitals.com
    uv run python scripts/brand_approaches/04_pgblank_pipeline.py URL --skip-llm
"""

from __future__ import annotations

import asyncio
import os
import time

from _common import build_url_parser, emit, normalize_url, status
from _pgblank.pipeline import run_pipeline


def main(argv: list[str] | None = None) -> int:
    parser = build_url_parser(
        "pgblank-style pipeline: Firecrawl ∥ dembrandt + LLM (local _pgblank)",
        extra=lambda p: p.add_argument(
            "--skip-llm",
            action="store_true",
            help="Crawl + dembrandt only (no brand-card LLM)",
        ),
    )
    args = parser.parse_args(argv)
    url = normalize_url(args.url)

    status(f"[04_pgblank_pipeline] {url} skip_llm={args.skip_llm}")
    if not os.environ.get("FIRECRAWL_BASE_URL"):
        status("FIRECRAWL_BASE_URL unset → defaulting to http://127.0.0.1:3002")

    started = time.perf_counter()
    result = asyncio.run(run_pipeline(url, skip_llm=args.skip_llm))
    warnings = [str(w) for w in (result.get("warnings") or [])]
    if result.get("error"):
        err = result["error"]
        if isinstance(err, dict) and err.get("message"):
            warnings.append(str(err["message"]))
    ok = result.get("status") in {"ok", "partial"}
    return emit(
        approach="04_pgblank_pipeline",
        url=url,
        started=started,
        result=result,
        warnings=warnings,
        ok=ok,
    )


if __name__ == "__main__":
    raise SystemExit(main())
