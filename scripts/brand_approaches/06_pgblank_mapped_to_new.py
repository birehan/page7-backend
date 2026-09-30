#!/usr/bin/env python3
"""Approach 06: pgblank-style export mapped/filled into new's onboarding proposal shape.

Thin wrap of ``scripts/extract_brand_via_pgblank.extract_via_pgblank``:
  Stage A — Firecrawl + dembrandt + LLM via ``brand_approaches/_pgblank`` (in-process)
  Stage B — map overlapping fields into new patch shape
  Stage C — fill remaining new schema fields via brand_research prompt

Lab-only (architecture forbids Firecrawl/Playwright in new's production path).

Needs: Firecrawl ``:3002``, dembrandt, ``backend/.env`` LLM keys.

Usage (from backend/):

    uv run python scripts/brand_approaches/06_pgblank_mapped_to_new.py https://obeidhospitals.com
    uv run python scripts/brand_approaches/06_pgblank_mapped_to_new.py URL --name Obeid
"""

from __future__ import annotations

import asyncio
import time

from _common import build_url_parser, emit, ensure_scripts_on_path, normalize_url, status


def main(argv: list[str] | None = None) -> int:
    parser = build_url_parser(
        "pgblank pipeline mapped to new proposal patch",
        extra=lambda p: p.add_argument(
            "--name",
            default=None,
            help="Optional brand name hint",
        ),
    )
    args = parser.parse_args(argv)
    url = normalize_url(args.url)

    ensure_scripts_on_path()
    from extract_brand_via_pgblank import extract_via_pgblank

    status(f"[06_pgblank_mapped_to_new] {url}")
    started = time.perf_counter()
    result = asyncio.run(extract_via_pgblank(url, brand_name=args.name))
    warnings = [str(w) for w in (result.get("warnings") or [])]
    ok = result.get("status") in {"succeeded", "partial"}
    return emit(
        approach="06_pgblank_mapped_to_new",
        url=url,
        started=started,
        result=result,
        warnings=warnings,
        ok=ok,
    )


if __name__ == "__main__":
    raise SystemExit(main())
