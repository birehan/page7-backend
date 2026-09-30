"""Brand research service — start SSE-over-jobs runs and inspect them."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.time import riyadh_day_start_utc, seconds_until_next_riyadh_midnight, utc_now
from app.features import brands
from app.features.brand_research import repository
from app.features.brand_research.schemas import GenerateBrandBody, RelearnBrandBody, RunOut
from app.infrastructure.ratelimit.limiter import RateLimiter
from app.jobs import queue as job_queue
from app.sse.bridge import bridge_sse_response, create_run
from app.sse.models import Run


def _inputs_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _normalize_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ApiError("VALIDATION", "Invalid website URL", status_code=422)
    # Drop fragment; keep path/query.
    path = parsed.path or "/"
    netloc = parsed.netloc.lower()
    query = f"?{parsed.query}" if parsed.query else ""
    return f"{parsed.scheme}://{netloc}{path}{query}"


async def _check_daily_cap(brand_id: uuid.UUID, settings: Settings) -> None:
    now = utc_now()
    limiter = RateLimiter()
    count = await limiter.increment(
        bucket=f"research:{brand_id}",
        now=now,
        window=timedelta(days=1),
        window_start=riyadh_day_start_utc(now),
    )
    if count > settings.research.daily_cap_per_brand:
        retry_after = seconds_until_next_riyadh_midnight(now)
        raise ApiError(
            "RATE_LIMIT",
            "Daily research limit reached for this brand",
            status_code=429,
            headers={"Retry-After": str(retry_after)},
        )


async def _start_research(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    user_name: str,
    brand_id: uuid.UUID,
    source_url: str,
    kind: str,
    decision_kind: str,
    idempotency_key: str | None,
    settings: Settings,
    brand_context_extra: dict[str, str] | None = None,
) -> StreamingResponse:
    from app.features import billing as billing_feature

    await billing_feature.enforce_ai_credit_limit(
        session, organization_id=organization_id
    )
    await _check_daily_cap(brand_id, settings)

    brand = await brands.get_brand(session, organization_id=organization_id, brand_id=brand_id)
    if brand is None:
        raise ApiError("NOT_FOUND", "Brand not found", status_code=404)

    normalized = _normalize_url(source_url)
    key = idempotency_key or f"{kind}:{brand_id}:{normalized}"
    payload_hash = _inputs_hash({"brandId": str(brand_id), "sourceUrl": normalized, "kind": kind})

    run = await create_run(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        kind=kind,
        idempotency_key=key,
        request_hash=payload_hash,
        created_by=user_id,
    )

    # Idempotent replay: research row may already exist for this run.
    research = await repository.get_research_run(session, research_run_id=run.id)
    if research is None:
        research = await repository.create_research_run(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            requested_by=user_id,
            source_url=normalized,
            run_id=run.id,
        )

    if run.status in {"queued"}:
        await job_queue.enqueue(
            session,
            queue="ai",
            type="ai.brand_research",
            payload={
                "run_id": str(run.id),
                "research_run_id": str(research.id),
                "organization_id": str(organization_id),
                "brand_id": str(brand_id),
                "user_id": str(user_id),
                "user_name": user_name,
                "source_url": normalized,
                "decision_kind": decision_kind,
                "brand_context": {
                    "name": brand.name,
                    "industry": brand.industry,
                    "city": brand.city,
                    **{
                        str(k): str(v)
                        for k, v in (brand_context_extra or {}).items()
                        if v and str(k) not in {"name", "industry", "city"}
                    },
                },
            },
            unique_key=f"brand_research:{run.id}",
            organization_id=organization_id,
        )
    await session.commit()
    return bridge_sse_response(run.id)


async def start_relearn(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    user_name: str,
    body: RelearnBrandBody,
    idempotency_key: str | None,
    settings: Settings,
) -> StreamingResponse:
    return await _start_research(
        session,
        organization_id=organization_id,
        user_id=user_id,
        user_name=user_name,
        brand_id=body.brand_id,
        source_url=str(body.url),
        kind="brand_relearn",
        decision_kind="brand_relearn",
        idempotency_key=idempotency_key,
        settings=settings,
    )


async def start_generate(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    user_name: str,
    body: GenerateBrandBody,
    idempotency_key: str | None,
    settings: Settings,
) -> StreamingResponse:
    brand = await brands.get_brand(session, organization_id=organization_id, brand_id=body.brand_id)
    if brand is None:
        raise ApiError("NOT_FOUND", "Brand not found", status_code=404)

    source = str(body.source_url) if body.source_url is not None else brand.website
    if not source:
        raise ApiError(
            "VALIDATION",
            "sourceUrl is required when the brand has no website",
            status_code=422,
        )

    return await _start_research(
        session,
        organization_id=organization_id,
        user_id=user_id,
        user_name=user_name,
        brand_id=body.brand_id,
        source_url=source,
        kind="brand_generate",
        decision_kind="brand_generate",
        idempotency_key=idempotency_key,
        settings=settings,
        brand_context_extra=body.brand_context,
    )


async def get_run(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    run_id: uuid.UUID,
) -> RunOut:
    run = (
        await session.execute(
            select(Run).where(Run.id == run_id, Run.organization_id == organization_id)
        )
    ).scalar_one_or_none()
    if run is None:
        raise ApiError("NOT_FOUND", "Run not found", status_code=404)
    return RunOut(
        id=run.id,
        organization_id=run.organization_id,
        brand_id=run.brand_id,
        kind=run.kind,
        status=run.status,
        result=run.result,
        error=run.error,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )
