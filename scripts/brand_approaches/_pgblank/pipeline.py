"""In-process pgblank-style pipeline: Firecrawl ∥ dembrandt (+ optional LLM via new).

Uses ``backend/.env`` ``LLM__*`` keys through ``app.integrations.llm`` — no separate
``OPENAI_API_KEY`` bridge and no ``/home/babi/pgblank/apps/api`` subprocess.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from .branding import MeasuredBranding, apply_measured_to_draft_fields
from .dembrandt import DembrandtExtractor
from .firecrawl import FirecrawlCrawler
from .types import AiUnavailableError, ConfigurationError, CrawlResult

_MARKDOWN_CAP = 12_000

_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "display_name",
        "tagline",
        "tone_description",
        "voice_traits",
        "color_palette",
        "logo_url",
        "pillars",
        "topics",
        "offer_hypotheses",
    ],
    "properties": {
        "display_name": {"type": "string"},
        "tagline": {"type": "string"},
        "tone_description": {"type": "string"},
        "voice_traits": {"type": "array", "items": {"type": "string"}},
        "color_palette": {"type": "array", "items": {"type": "string"}},
        "logo_url": {"type": ["string", "null"]},
        "pillars": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name_en", "name_ar", "description"],
                "properties": {
                    "name_en": {"type": "string"},
                    "name_ar": {"type": "string"},
                    "description": {"type": "string"},
                },
            },
        },
        "topics": {"type": "array", "items": {"type": "string"}},
        "offer_hypotheses": {"type": "array", "items": {"type": "string"}},
    },
}


def _empty_crawl() -> CrawlResult:
    return CrawlResult(url="", title="", markdown="", metadata={}, branding={})


async def run_pipeline(url: str, *, skip_llm: bool = False) -> dict[str, Any]:
    """Firecrawl ∥ dembrandt, optionally draft a brand card with new's LLM router."""
    started = time.perf_counter()
    crawler = FirecrawlCrawler()
    branding = DembrandtExtractor()
    warnings: list[str] = []

    crawl_started = time.perf_counter()
    crawl_exc: Exception | None = None
    try:
        result, measured = await asyncio.gather(
            crawler.crawl(url),
            branding.extract(url),
        )
    except (AiUnavailableError, ConfigurationError) as exc:
        crawl_exc = exc
        measured = await branding.extract(url)
        result = _empty_crawl()
        warnings.append(f"firecrawl failed: {exc}")
    crawl_seconds = time.perf_counter() - crawl_started

    if not branding.available:
        warnings.append(
            "dembrandt binary not found — measured colors/logo/fonts will be empty "
            "(install via pgblank tools/brand-extract-compare)"
        )

    payload: dict[str, Any] = {
        "status": "partial" if crawl_exc or not result.markdown.strip() else "ok",
        "approach": "pgblank_measured_llm_local",
        "url": url,
        "crawler": type(crawler).__name__,
        "branding_extractor": type(branding).__name__,
        "dembrandt_available": branding.available,
        "firecrawl_base_url": crawler.base_url,
        "crawl": {
            "title": result.title,
            "markdown_len": len(result.markdown),
            "markdown": result.markdown[:_MARKDOWN_CAP],
            "markdown_preview": result.markdown[:500],
            "metadata": dict(result.metadata) if result.metadata else {},
        },
        "measured": {
            "colors": list(measured.colors),
            "logo_url": measured.logo_url,
            "fonts": list(measured.fonts),
            "color_scheme": measured.color_scheme,
        },
        "draft": None,
        "timing": {
            "crawl_and_measure_seconds": round(crawl_seconds, 3),
            "draft_seconds": None,
            "elapsed_seconds": None,
        },
        "warnings": warnings,
    }

    if not result.markdown.strip() and crawl_exc is None:
        payload["warnings"].append("empty crawl markdown")
        payload["status"] = "partial"

    if skip_llm:
        payload["warnings"].append("llm skipped (--skip-llm)")
        payload["timing"]["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        return payload

    draft_started = time.perf_counter()
    try:
        draft = await _draft_with_new_llm(url=url, crawl=result, measured=measured)
        overlay = apply_measured_to_draft_fields(
            measured=measured,
            display_name=str(draft.get("display_name") or ""),
            tagline=str(draft.get("tagline") or ""),
            color_palette=tuple(str(c) for c in (draft.get("color_palette") or [])),
            logo_url=draft.get("logo_url") if isinstance(draft.get("logo_url"), str) else None,
            site_title=result.title,
            site_description=str(result.metadata.get("description", "") or ""),
        )
        draft["display_name"] = overlay["display_name"]
        draft["tagline"] = overlay["tagline"]
        draft["color_palette"] = list(overlay["color_palette"])  # type: ignore[arg-type]
        draft["logo_url"] = overlay["logo_url"]
        payload["draft"] = draft
        payload["text_generator"] = "new_llm_router"
    except Exception as exc:  # noqa: BLE001 — keep crawl/measured on LLM failure
        payload["status"] = "partial"
        payload["error"] = {"code": "LLM_FAILED", "message": str(exc)}
        payload["warnings"].append(f"llm failed: {exc}")
        payload["timing"]["draft_seconds"] = round(time.perf_counter() - draft_started, 3)
        payload["timing"]["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        return payload

    payload["timing"]["draft_seconds"] = round(time.perf_counter() - draft_started, 3)
    payload["timing"]["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    if not str(draft.get("tone_description") or "").strip() and not draft.get("voice_traits"):
        payload["status"] = "partial"
        payload["warnings"].append("empty tone/voice draft")
    return payload


async def _draft_with_new_llm(
    *,
    url: str,
    crawl: CrawlResult,
    measured: MeasuredBranding,
) -> dict[str, Any]:
    from app.core.config import get_settings
    from app.integrations.llm import get_llm_router
    from app.integrations.llm.ports import StructuredGenerationRequest

    settings = get_settings()
    llm = get_llm_router(settings)

    measured_lines = []
    if crawl.title:
        measured_lines.append(f"site_title: {crawl.title}")
    desc = crawl.metadata.get("description")
    if desc:
        measured_lines.append(f"site_description: {desc}")
    if measured.colors:
        measured_lines.append(f"measured_colors: {', '.join(measured.colors)}")
    if measured.logo_url:
        measured_lines.append(f"logo_url: {measured.logo_url}")

    markdown = crawl.markdown[:_MARKDOWN_CAP] or "(no markdown)"
    user = (
        "Draft a bilingual-ready brand card from SITE evidence. "
        "Ground everything in the evidence; do not invent specifics.\n\n"
        f"URL: {url}\n"
    )
    if measured_lines:
        user += "MEASURED branding (ground truth):\n" + "\n".join(measured_lines) + "\n\n"
    user += f"SITE markdown:\n{markdown}"

    response = await llm.run_structured(
        "brand_research",
        StructuredGenerationRequest(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a brand strategist for a small Saudi business. "
                        "Return JSON matching the schema only."
                    ),
                },
                {"role": "user", "content": user},
            ],
            output_schema=_DRAFT_SCHEMA,
            schema_name="pgblank_brand_card_lab",
            temperature=0.2,
        ),
    )
    raw = response.content
    if isinstance(raw, str):
        data = json.loads(raw)
    elif isinstance(raw, dict):
        data = raw
    else:
        data = json.loads(str(raw))
    if not isinstance(data, dict):
        raise RuntimeError("LLM returned non-object draft")
    return data
