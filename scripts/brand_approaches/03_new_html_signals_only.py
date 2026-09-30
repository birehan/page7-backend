#!/usr/bin/env python3
"""Approach 03: new HTML signals only (no LLM).

Uses first-party ``fetch_site`` + per-page ``extract_page`` signals (title, og,
theme_color, logo_url, social/contact links, text length). Cheap baseline for
comparing visual/meta extraction before LLM enrichment.

Needs: outbound HTTPS only (no LLM keys).

Usage (from backend/):

    uv run python scripts/brand_approaches/03_new_html_signals_only.py https://www.extra.com/
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict
from typing import Any

from _common import build_url_parser, emit, normalize_url, status


def _page_summary(page: Any) -> dict[str, Any]:
    return {
        "url": page.url,
        "title": page.title,
        "text_len": len(page.text or ""),
        "text_preview": (page.text or "")[:400],
        "og": dict(page.og or {}),
        "theme_color": page.theme_color,
        "logo_url": page.logo_url,
        "language": page.language,
        "social_links": list(page.social_links or []),
        "contact_links": list(page.contact_links or []),
        "json_ld_count": len(page.json_ld or []),
        "same_site_links_count": len(page.same_site_links or []),
    }


async def _run(url: str) -> dict[str, Any]:
    from app.integrations.webfetch.fetcher import fetch_site

    site = await fetch_site(url)
    pages = [_page_summary(p) for p in site.pages]
    outcomes = [
        {
            "url": o.url,
            "robots_disallowed": o.robots_disallowed,
            "timed_out": o.timed_out,
            "error": o.error,
            "ok": o.extraction is not None,
        }
        for o in site.outcomes
    ]
    logos = [p["logo_url"] for p in pages if p.get("logo_url")]
    themes = [p["theme_color"] for p in pages if p.get("theme_color")]
    return {
        "source_url": site.source_url,
        "crawled_pages": site.crawled_pages,
        "content_hash": site.content_hash,
        "warnings": list(site.warnings),
        "logos": logos,
        "theme_colors": themes,
        "pages": pages,
        "outcomes": outcomes,
        # Keep a tiny serializable peek at dataclasses if useful later.
        "page_keys": sorted(asdict(site.pages[0]).keys()) if site.pages else [],
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_url_parser("new HTML signals only (fetch_site, no LLM)")
    args = parser.parse_args(argv)
    url = normalize_url(args.url)

    status(f"[03_new_html_signals_only] {url}")
    started = time.perf_counter()
    try:
        result = asyncio.run(_run(url))
    except Exception as exc:  # noqa: BLE001 — lab CLI: surface as envelope
        return emit(
            approach="03_new_html_signals_only",
            url=url,
            started=started,
            result={"error": {"code": "EXCEPTION", "message": str(exc)}},
            warnings=[str(exc)],
            ok=False,
        )

    warnings = list(result.get("warnings") or [])
    ok = bool(result.get("crawled_pages"))
    if not ok and not warnings:
        warnings.append("no pages crawled")
    return emit(
        approach="03_new_html_signals_only",
        url=url,
        started=started,
        result=result,
        warnings=warnings,
        ok=ok,
    )


if __name__ == "__main__":
    raise SystemExit(main())
