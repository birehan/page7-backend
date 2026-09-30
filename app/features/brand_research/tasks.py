"""Orchestrate brand research for one run (fast default, or full fetch+search)."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.brand_research import repository
from app.features.brand_research.exceptions import most_specific_code
from app.features.brand_research.fast_extract import (
    PROMPT_VERSION as FAST_PROMPT_VERSION,
)
from app.features.brand_research.fast_extract import (
    FastExtractResearcher,
)
from app.features.brand_research.fetch_extract import FetchExtractResearcher
from app.features.brand_research.merge import (
    field_confidence_map,
    merge,
    to_proposal_patch,
)
from app.features.brand_research.models import BrandResearchRun
from app.features.brand_research.ports import (
    EXTRACTION_FIELD_NAMES,
    ExtractionResult,
    ResearchRequest,
)
from app.features.brand_research.prompts import extraction as extraction_prompt
from app.features.brand_research.search_augmented import SearchAugmentedResearcher
from app.features.content_ai import insert_decision
from app.integrations.llm.router import LLMTaskRouter

# Hard ceiling for the search stage so a hung provider tool call cannot burn the
# full job lease when first-party fetch already produced usable content.
_SEARCH_STAGE_TIMEOUT_SECONDS = 90.0


def _has_any_field(result: ExtractionResult) -> bool:
    return any(getattr(result, name).confidence is not None for name in EXTRACTION_FIELD_NAMES)


def _proposal_looks_fake(proposal: dict[str, Any]) -> bool:
    """Reject content-hash short-circuit of FakeLLM / placeholder research."""
    sources = proposal.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, str) and "example.sa" in source:
                return True
    patch = proposal.get("patch")
    if isinstance(patch, dict):
        guidelines = patch.get("guidelines")
        if isinstance(guidelines, dict):
            # Walk nested source lists if present on field shapes.
            for value in guidelines.values():
                if isinstance(value, dict):
                    urls = value.get("source_page_urls") or value.get("sourcePageUrls")
                    if isinstance(urls, list) and any(
                        isinstance(u, str) and "example.sa" in u for u in urls
                    ):
                        return True
    return False


async def _research_with_timeout(
    researcher: SearchAugmentedResearcher,
    request: ResearchRequest,
) -> ExtractionResult:
    """Run search-augmented research, degrading to warnings on timeout/provider errors."""
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
    except (
        ProviderTimeoutError,
        ProviderUnavailableError,
        ProviderRateLimitedError,
    ) as exc:
        return ExtractionResult(warnings=[f"search failed: {exc}"])
    except Exception as exc:  # noqa: BLE001 — degrade search; fetch may still succeed
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


def _fail_codes_from_warnings(warnings: list[str]) -> list[str]:
    codes: list[str] = []
    for warning in warnings:
        lower = warning.lower()
        if "robots" in lower:
            codes.append("RESEARCH_ROBOTS_DISALLOWED")
        elif "timed out" in lower:
            codes.append("RESEARCH_FETCH_TIMEOUT")
        elif "fetch failed" in lower or "could not fetch" in lower:
            codes.append("RESEARCH_FETCH_FAILED")
    return codes


async def _try_content_hash_short_circuit(
    session: AsyncSession,
    *,
    research: BrandResearchRun,
    brand_id: uuid.UUID,
    source_url: str,
    content_hash: str | None,
    crawled_pages: int,
    approach: str,
) -> dict[str, Any] | None:
    if not content_hash:
        return None
    previous = await repository.find_previous_succeeded(
        session, brand_id=brand_id, source_url=source_url
    )
    prev_hash = None
    if previous is not None and isinstance(previous.extracted, dict):
        prev_hash = previous.extracted.get("content_hash")
    if (
        previous is None
        or not prev_hash
        or prev_hash != content_hash
        or not isinstance(previous.proposal, dict)
        or _proposal_looks_fake(previous.proposal)
    ):
        return None

    proposal = dict(previous.proposal)
    warnings = list(proposal.get("warnings") or [])
    if "no changes since last research" not in warnings:
        warnings.append("no changes since last research")
    proposal_event = {
        "type": "proposal",
        "patch": proposal.get("patch") or to_proposal_patch(ExtractionResult()),
        "confidence": proposal.get("confidence") or {},
        "sources": proposal.get("sources") or [],
        "warnings": warnings,
    }
    if "patch" not in proposal and "guidelines" in proposal:
        proposal_event["patch"] = {k: v for k, v in proposal.items() if k != "warnings"}
    stored = {
        "patch": proposal_event["patch"],
        "confidence": proposal_event["confidence"],
        "sources": proposal_event["sources"],
        "warnings": warnings,
    }
    await repository.finalize_run(
        session,
        research,
        status="succeeded",
        crawled_pages=crawled_pages,
        extracted={
            "content_hash": content_hash,
            "short_circuited": True,
            "approach": approach,
        },
        proposal=stored,
        decision_id=None,
    )
    return {
        "status": "succeeded",
        "proposal_event": proposal_event,
        "decision_id": None,
        "short_circuited": True,
    }


async def _run_fast_pipeline(
    session: AsyncSession,
    *,
    research: BrandResearchRun,
    request: ResearchRequest,
    decision_kind: str,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    user_id: uuid.UUID,
    llm_router: LLMTaskRouter,
) -> dict[str, Any]:
    approach = "fast"
    researcher = FastExtractResearcher(llm_router)
    result = await researcher.research(request)

    short = await _try_content_hash_short_circuit(
        session,
        research=research,
        brand_id=brand_id,
        source_url=request.source_url,
        content_hash=researcher.last_content_hash,
        crawled_pages=researcher.last_crawled_pages,
        approach=approach,
    )
    if short is not None:
        return short

    if not _has_any_field(result):
        code = most_specific_code(
            _fail_codes_from_warnings(list(result.warnings)) or ["RESEARCH_NO_CONTENT"]
        )
        await repository.finalize_run(
            session,
            research,
            status="failed",
            crawled_pages=researcher.last_crawled_pages,
            extracted={
                "content_hash": researcher.last_content_hash,
                "approach": approach,
                "fetch_warnings": result.warnings,
            },
            error_message=code,
        )
        return {
            "status": "failed",
            "error": {"code": code, "message": "No extractable brand information found"},
        }

    patch = to_proposal_patch(result)
    confidence = field_confidence_map(result)
    sources = list(result.sources)
    warnings = list(result.warnings)
    status = "partial" if warnings and _has_any_field(result) else "succeeded"

    task_cfg = get_settings().llm.tasks["brand_research"]
    decision = await insert_decision(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_user_id=user_id,
        kind=decision_kind,
        provider=task_cfg.provider,
        model=task_cfg.model,
        prompt_version=FAST_PROMPT_VERSION,
        inputs_hash=request.source_url,
        input_summary={
            "sourceUrl": request.source_url,
            "brandContext": request.brand_context,
            "approach": approach,
        },
        output={"patch": patch, "confidence": confidence, "sources": sources},
        status=status if status != "partial" else "partial",
        target_type="brand",
        target_id=brand_id,
        prompt_tokens=0,
        completion_tokens=0,
    )

    stored_proposal = {
        "patch": patch,
        "confidence": confidence,
        "sources": sources,
        "warnings": warnings,
    }
    await repository.finalize_run(
        session,
        research,
        status=status,
        crawled_pages=researcher.last_crawled_pages,
        extracted={
            "content_hash": researcher.last_content_hash,
            "approach": approach,
            "timing": researcher.last_timing,
            "fetch_warnings": result.warnings,
        },
        proposal=stored_proposal,
        decision_id=decision.id,
    )
    return {
        "status": status,
        "proposal_event": {
            "type": "proposal",
            "patch": patch,
            "confidence": confidence,
            "sources": sources,
            "warnings": warnings,
        },
        "decision_id": str(decision.id),
    }


async def _run_full_pipeline(
    session: AsyncSession,
    *,
    research: BrandResearchRun,
    request: ResearchRequest,
    decision_kind: str,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    user_id: uuid.UUID,
    llm_router: LLMTaskRouter,
) -> dict[str, Any]:
    approach = "full"
    fetch_researcher = FetchExtractResearcher(llm_router)
    search_researcher = SearchAugmentedResearcher(llm_router)

    fetch_result = await fetch_researcher.research(request)
    site = fetch_researcher.last_site

    short = await _try_content_hash_short_circuit(
        session,
        research=research,
        brand_id=brand_id,
        source_url=request.source_url,
        content_hash=site.content_hash if site is not None else None,
        crawled_pages=site.crawled_pages if site is not None else 0,
        approach=approach,
    )
    if short is not None:
        return short

    search_result = await _research_with_timeout(search_researcher, request)
    merged = merge(fetch_result, search_result)

    if not _has_any_field(merged):
        code = most_specific_code(
            _fail_codes_from_warnings([*fetch_result.warnings, *search_result.warnings])
            or ["RESEARCH_NO_CONTENT"]
        )
        await repository.finalize_run(
            session,
            research,
            status="failed",
            crawled_pages=site.crawled_pages if site else 0,
            extracted={
                "content_hash": site.content_hash if site else None,
                "approach": approach,
                "fetch_warnings": fetch_result.warnings,
                "search_warnings": search_result.warnings,
            },
            error_message=code,
        )
        return {
            "status": "failed",
            "error": {"code": code, "message": "No extractable brand information found"},
        }

    patch = to_proposal_patch(merged)
    confidence = field_confidence_map(merged)
    sources = list(merged.sources)
    warnings = list(merged.warnings)
    status = "partial" if _is_partial(fetch_result, search_result, merged) else "succeeded"

    task_cfg = get_settings().llm.tasks["brand_research"]
    decision = await insert_decision(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_user_id=user_id,
        kind=decision_kind,
        provider=task_cfg.provider,
        model=task_cfg.model,
        prompt_version=extraction_prompt.PROMPT_VERSION,
        inputs_hash=request.source_url,
        input_summary={
            "sourceUrl": request.source_url,
            "brandContext": request.brand_context,
            "approach": approach,
        },
        output={"patch": patch, "confidence": confidence, "sources": sources},
        status=status if status != "partial" else "partial",
        target_type="brand",
        target_id=brand_id,
        prompt_tokens=0,
        completion_tokens=0,
    )

    stored_proposal = {
        "patch": patch,
        "confidence": confidence,
        "sources": sources,
        "warnings": warnings,
    }
    await repository.finalize_run(
        session,
        research,
        status=status,
        crawled_pages=site.crawled_pages if site else 0,
        extracted={
            "content_hash": site.content_hash if site else None,
            "approach": approach,
            "fetch_warnings": fetch_result.warnings,
            "search_warnings": search_result.warnings,
        },
        proposal=stored_proposal,
        decision_id=decision.id,
    )
    return {
        "status": status,
        "proposal_event": {
            "type": "proposal",
            "patch": patch,
            "confidence": confidence,
            "sources": sources,
            "warnings": warnings,
        },
        "decision_id": str(decision.id),
    }


async def run_research_pipeline(
    session: AsyncSession,
    *,
    research: BrandResearchRun,
    source_url: str,
    brand_context: dict[str, str],
    decision_kind: str,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    user_id: uuid.UUID,
    llm_router: LLMTaskRouter,
) -> dict[str, Any]:
    """Execute research for one run; approach from RESEARCH__APPROACH (default fast).

    Raises nothing for recoverable stage failures — returns a failed/partial
    outcome dict the caller emits.
    """
    request = ResearchRequest(source_url=source_url, brand_context=brand_context)
    approach = get_settings().research.approach
    if approach == "full":
        return await _run_full_pipeline(
            session,
            research=research,
            request=request,
            decision_kind=decision_kind,
            organization_id=organization_id,
            brand_id=brand_id,
            user_id=user_id,
            llm_router=llm_router,
        )
    return await _run_fast_pipeline(
        session,
        research=research,
        request=request,
        decision_kind=decision_kind,
        organization_id=organization_id,
        brand_id=brand_id,
        user_id=user_id,
        llm_router=llm_router,
    )
