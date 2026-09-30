#!/usr/bin/env python3
"""Approach 05: dembrandt measured branding only (colors / logo / fonts).

Uses vendored ``_pgblank.DembrandtExtractor`` — no pgblank apps/api subprocess,
no OpenAI key.

Needs: dembrandt CLI + Chromium (resolved from PATH or pgblank tools/node_modules).

Usage (from backend/):

    uv run python scripts/brand_approaches/05_pgblank_dembrandt_only.py https://obeidhospitals.com
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from _common import build_url_parser, emit, normalize_url, status
from _pgblank.dembrandt import DembrandtExtractor


async def _run_measured(url: str) -> dict[str, Any]:
    extractor = DembrandtExtractor()
    warnings: list[str] = []
    if not extractor.available:
        warnings.append(
            "dembrandt binary not found — install Chromium via "
            "pnpm --filter @pgblank/brand-extract-compare exec dembrandt install-browser"
        )

    measured = await extractor.extract(url)
    has_signal = bool(measured.colors or measured.logo_url or measured.fonts)
    if not has_signal:
        warnings.append("empty measured branding")

    return {
        "status": "ok" if has_signal else "partial",
        "branding_extractor": type(extractor).__name__,
        "dembrandt_available": extractor.available,
        "measured": {
            "colors": list(measured.colors),
            "logo_url": measured.logo_url,
            "fonts": list(measured.fonts),
            "color_scheme": measured.color_scheme,
        },
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_url_parser("dembrandt measured branding only (local _pgblank)")
    args = parser.parse_args(argv)
    url = normalize_url(args.url)

    status(f"[05_pgblank_dembrandt_only] {url}")
    started = time.perf_counter()
    result = asyncio.run(_run_measured(url))
    warnings = [str(w) for w in (result.get("warnings") or [])]
    # Soft-ok when dembrandt ran (even if empty); fail hard only when binary missing.
    ok = bool(result.get("dembrandt_available"))
    return emit(
        approach="05_pgblank_dembrandt_only",
        url=url,
        started=started,
        result=result,
        warnings=warnings,
        ok=ok,
    )


if __name__ == "__main__":
    raise SystemExit(main())
