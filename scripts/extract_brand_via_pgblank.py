#!/usr/bin/env python3
"""Extract brand fields via pgblank-style pipeline, mapped to new's proposal shape.

Stage A: Firecrawl + dembrandt + LLM brand card (in-process via brand_approaches/_pgblank).
Stage B: map overlapping fields into new's patch shape.
Stage C: fill do/dont/banned/dialect/languages/competitors with new's brand_research schema.

Lab-only — does not change production onboarding (architecture/08 forbids Firecrawl/
Playwright in new's research path).

Usage (from backend/):

    # Firecrawl must be reachable on :3002
    uv run python scripts/extract_brand_via_pgblank.py https://obeidhospitals.com
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from app.core.config import get_settings
from app.features.brand_research.merge import field_confidence_map, to_proposal_patch
from app.features.brand_research.parse import parse_extraction_content
from app.features.brand_research.ports import (
    EXTRACTION_FIELD_NAMES,
    ExtractionResult,
    FieldExtraction,
)
from app.features.brand_research.prompts import extraction as extraction_prompt
from app.integrations.llm import get_llm_router
from app.integrations.llm.ports import StructuredGenerationRequest

_MARKDOWN_CHAR_CAP = 12_000
_BRAND_APPROACHES = Path(__file__).resolve().parent / "brand_approaches"


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


async def _run_pgblank_export(url: str) -> dict[str, Any]:
    """In-process Firecrawl ∥ dembrandt + LLM via brand_approaches/_pgblank."""
    approaches = str(_BRAND_APPROACHES)
    if approaches not in sys.path:
        sys.path.insert(0, approaches)
    from _pgblank.pipeline import run_pipeline

    return cast(dict[str, Any], await run_pipeline(url, skip_llm=False))


def _map_pillars(raw_pillars: list[dict[str, Any]] | None) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for item in raw_pillars or []:
        name_ar = str(item.get("name_ar") or "").strip()
        name_en = str(item.get("name_en") or "").strip()
        name = name_ar or name_en
        desc = str(item.get("description") or "").strip()
        if name:
            out.append({"name": name, "description": desc})
    return out


def _field(
    value: Any,
    confidence: float,
    *,
    sources: list[str] | None = None,
) -> FieldExtraction:
    return FieldExtraction(
        value=value,
        confidence=confidence,
        source_page_urls=list(sources or []),
    )


def _mapped_from_pgblank(
    *,
    url: str,
    draft: dict[str, Any],
    measured: dict[str, Any],
) -> ExtractionResult:
    """Map pgblank card + measured branding into ExtractionResult (partial)."""
    sources = [url]
    colors = list(draft.get("color_palette") or measured.get("colors") or [])
    # Prefer hex-looking colors only for new's schema preference.
    hex_colors = [c for c in colors if isinstance(c, str) and c.startswith("#")]
    if not hex_colors and colors:
        hex_colors = [str(c) for c in colors]

    voice = list(draft.get("voice_traits") or [])
    pillars = _map_pillars(draft.get("pillars") if isinstance(draft.get("pillars"), list) else None)
    logo = draft.get("logo_url") or measured.get("logo_url")

    data: dict[str, FieldExtraction] = {name: FieldExtraction() for name in EXTRACTION_FIELD_NAMES}
    if voice:
        data["voice_adjectives"] = _field(voice, 0.75, sources=sources)
    if hex_colors:
        color_conf = 0.95 if measured.get("colors") else 0.7
        data["colors"] = _field(hex_colors, color_conf, sources=sources)
    if pillars:
        data["pillars_suggested"] = _field(pillars, 0.7, sources=sources)
    if logo:
        data["logo_url"] = _field(str(logo), 0.9, sources=sources)

    return ExtractionResult(**data, sources=sources, warnings=[])


async def _fill_new_schema(
    *,
    url: str,
    brand_name: str | None,
    crawl: dict[str, Any],
    measured: dict[str, Any],
    draft: dict[str, Any],
) -> ExtractionResult:
    """Run new's brand_research structured extract over Firecrawl markdown."""
    settings = get_settings()
    llm = get_llm_router(settings)
    title = str(crawl.get("title") or "")
    markdown = str(crawl.get("markdown_preview") or "")
    # Prefer full markdown if export ever adds it; fall back to preview.
    full_md = crawl.get("markdown")
    if isinstance(full_md, str) and full_md.strip():
        markdown = full_md
    text = markdown[:_MARKDOWN_CHAR_CAP]

    brand_context: dict[str, str] = {}
    if brand_name:
        brand_context["name"] = brand_name
    if draft.get("display_name"):
        brand_context["display_name"] = str(draft["display_name"])
    if draft.get("tagline"):
        brand_context["tagline"] = str(draft["tagline"])

    page_summaries = [
        {
            "url": url,
            "title": title,
            "text": text,
            "og": {},
            "theme_color": (measured.get("colors") or [None])[0],
            "language": "",
            "social_links": [],
            "contact_links": [],
            "json_ld": [],
            "logo_url": measured.get("logo_url") or draft.get("logo_url"),
        }
    ]
    messages = extraction_prompt.build(
        source_url=url,
        brand_context=brand_context,
        page_summaries=page_summaries,
    )
    response = await llm.run_structured(
        "brand_research",
        StructuredGenerationRequest(
            messages=messages,
            output_schema=extraction_prompt.OUTPUT_SCHEMA,
            schema_name="brand_research",
            temperature=0.2,
        ),
    )
    return parse_extraction_content(
        response.content,
        fallback_sources=[url],
        extra_warnings=[],
    )


def _merge_prefer_mapped(mapped: ExtractionResult, filled: ExtractionResult) -> ExtractionResult:
    """Prefer pgblank-mapped measured/voice/pillars/logo; use new fill for gaps."""
    merged: dict[str, FieldExtraction] = {}
    for name in EXTRACTION_FIELD_NAMES:
        m: FieldExtraction = getattr(mapped, name)
        f: FieldExtraction = getattr(filled, name)
        if m.confidence is not None:
            merged[name] = m
        elif f.confidence is not None:
            merged[name] = f
        else:
            merged[name] = FieldExtraction()
    sources: list[str] = []
    seen: set[str] = set()
    for url in [*mapped.sources, *filled.sources]:
        if url not in seen:
            seen.add(url)
            sources.append(url)
    warnings = [*mapped.warnings, *filled.warnings]
    return ExtractionResult(**merged, sources=sources, warnings=warnings)


async def extract_via_pgblank(
    url: str,
    *,
    brand_name: str | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    warnings: list[str] = []

    pgblank = await _run_pgblank_export(url)
    timing_pg = dict(pgblank.get("timing") or {})
    crawl = dict(pgblank.get("crawl") or {})
    measured = dict(pgblank.get("measured") or {})
    draft = dict(pgblank.get("draft") or {})
    warnings.extend(str(w) for w in (pgblank.get("warnings") or []))

    # Export currently only ships markdown_preview — pull length hint into crawl.
    if "markdown" not in crawl and crawl.get("markdown_preview"):
        crawl["markdown"] = crawl["markdown_preview"]

    mapped = _mapped_from_pgblank(url=url, draft=draft, measured=measured)

    fill_seconds: float | None = None
    filled = ExtractionResult(warnings=["new schema fill skipped"])
    if crawl.get("markdown_preview") or crawl.get("markdown"):
        fill_started = time.perf_counter()
        try:
            filled = await _fill_new_schema(
                url=url,
                brand_name=brand_name,
                crawl=crawl,
                measured=measured,
                draft=draft,
            )
        except Exception as exc:  # noqa: BLE001 — keep mapped fields on fill failure
            warnings.append(f"new schema fill failed: {exc}")
            filled = ExtractionResult(warnings=[f"new schema fill failed: {exc}"])
        fill_seconds = time.perf_counter() - fill_started
    else:
        warnings.append("no Firecrawl markdown for new schema fill")

    merged = _merge_prefer_mapped(mapped, filled)
    warnings.extend(merged.warnings)

    elapsed = round(time.perf_counter() - started, 3)
    timing = {
        "pgblank_crawl_measure_seconds": timing_pg.get("crawl_and_measure_seconds"),
        "pgblank_draft_seconds": timing_pg.get("draft_seconds"),
        "new_schema_fill_seconds": (None if fill_seconds is None else round(fill_seconds, 3)),
        "pgblank_total_seconds": timing_pg.get("elapsed_seconds"),
    }

    extras = {
        "display_name": draft.get("display_name") or None,
        "tagline": draft.get("tagline") or None,
        "tone_description": draft.get("tone_description") or None,
        "fonts": measured.get("fonts") or None,
        "color_scheme": measured.get("color_scheme") or None,
        "topics": draft.get("topics") or None,
        "offer_hypotheses": draft.get("offer_hypotheses") or None,
        "crawler": pgblank.get("crawler"),
        "branding_extractor": pgblank.get("branding_extractor"),
    }

    if not _has_any_field(merged):
        return {
            "status": "failed",
            "approach": "pgblank_default_mapped",
            "error": pgblank.get("error")
            or {"code": "RESEARCH_NO_CONTENT", "message": "No extractable brand fields"},
            "warnings": warnings,
            "pgblank_extras": extras,
            "elapsed_seconds": elapsed,
            "timing": timing,
        }

    status = "partial" if warnings or pgblank.get("status") != "ok" else "succeeded"
    if not _has_any_field(filled) and _has_any_field(mapped):
        status = "partial"

    return {
        "status": status,
        "type": "proposal",
        "approach": "pgblank_default_mapped",
        "patch": to_proposal_patch(merged),
        "confidence": field_confidence_map(merged),
        "sources": list(merged.sources),
        "warnings": warnings,
        "pgblank_extras": extras,
        "elapsed_seconds": elapsed,
        "timing": timing,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run pgblank default extract (Firecrawl+dembrandt+LLM), map to new "
            "onboarding fields, fill remaining fields with new's schema. Logs timing."
        ),
    )
    parser.add_argument("url", help="Brand website URL")
    parser.add_argument("--name", default=None, help="Optional brand name hint")
    args = parser.parse_args(argv)
    url = _normalize_url(args.url)

    print(f"extracting via pgblank default → new fields: {url}", file=sys.stderr, flush=True)
    result = asyncio.run(extract_via_pgblank(url, brand_name=args.name))
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")

    timing = result.get("timing") or {}
    print(
        f"elapsed_seconds={result.get('elapsed_seconds')} "
        f"crawl_measure={timing.get('pgblank_crawl_measure_seconds')} "
        f"pgblank_draft={timing.get('pgblank_draft_seconds')} "
        f"new_fill={timing.get('new_schema_fill_seconds')}",
        file=sys.stderr,
        flush=True,
    )
    return 0 if result.get("status") in {"succeeded", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
