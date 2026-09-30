#!/usr/bin/env python3
"""CLI wrapper for FastExtractResearcher (app.features.brand_research.fast_extract).

Usage (from backend/):

    uv run python scripts/fast_brand_extract.py https://obeidhospitals.com
    uv run python scripts/fast_brand_extract.py https://stripe.com --name Stripe
    uv run python scripts/fast_brand_extract.py https://x.com --html /tmp/x.html
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from urllib.parse import urlparse

from app.features.brand_research.fast_extract import fast_extract

__all__ = ["fast_extract", "main"]


def _normalise_url(raw: str) -> str:
    text = raw.strip()
    if "://" not in text:
        text = f"https://{text}"
    p = urlparse(text)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise SystemExit(f"error: invalid URL: {raw!r}")
    return p.geturl()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Fast brand extraction (<30s), all 10 brand fields. "
            "No Firecrawl or Playwright required."
        )
    )
    parser.add_argument("url", help="Brand website URL")
    parser.add_argument("--name", default=None, metavar="NAME", help="Brand name hint")
    parser.add_argument(
        "--html",
        default=None,
        metavar="FILE",
        help="Dev: skip live fetch, use this HTML file",
    )
    args = parser.parse_args(argv)

    url = _normalise_url(args.url)
    print(f"fast-extract v2: {url}", file=sys.stderr, flush=True)

    result = asyncio.run(fast_extract(url, brand_name=args.name, html_file=args.html))
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")

    t = result.get("timing", {})
    status = result.get("status", "?")
    fields_n = len(result.get("fields", {}))
    print(
        f"\nstatus={status}  fields={fields_n}/10  "
        f"elapsed={result.get('elapsed_seconds')}s  "
        f"fetch={t.get('fetch_homepage_seconds')}s  "
        f"llm={t.get('llm_seconds')}s  "
        f"pages={t.get('pages_fetched')}",
        file=sys.stderr,
        flush=True,
    )
    return 0 if status in {"succeeded", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
