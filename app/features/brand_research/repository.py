"""Persistence for brand_research_runs."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.features.brand_research.models import BrandResearchRun


async def create_research_run(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    requested_by: uuid.UUID,
    source_url: str,
    run_id: uuid.UUID | None = None,
) -> BrandResearchRun:
    row = BrandResearchRun(
        organization_id=organization_id,
        brand_id=brand_id,
        requested_by=requested_by,
        source_url=source_url,
        status="queued",
    )
    if run_id is not None:
        row.id = run_id
    session.add(row)
    await session.flush()
    return row


async def get_research_run(
    session: AsyncSession, *, research_run_id: uuid.UUID
) -> BrandResearchRun | None:
    return (
        await session.execute(
            select(BrandResearchRun).where(BrandResearchRun.id == research_run_id)
        )
    ).scalar_one_or_none()


async def find_previous_succeeded(
    session: AsyncSession,
    *,
    brand_id: uuid.UUID,
    source_url: str,
) -> BrandResearchRun | None:
    """Latest succeeded/partial run for content-hash short-circuit lookup."""
    result = await session.execute(
        select(BrandResearchRun)
        .where(
            BrandResearchRun.brand_id == brand_id,
            BrandResearchRun.source_url == source_url,
            BrandResearchRun.status.in_(("succeeded", "partial")),
            BrandResearchRun.proposal.is_not(None),
        )
        .order_by(BrandResearchRun.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def mark_running(session: AsyncSession, row: BrandResearchRun) -> None:
    if row.status == "queued":
        row.status = "running"
        row.started_at = utc_now()
        await session.flush()


async def finalize_run(
    session: AsyncSession,
    row: BrandResearchRun,
    *,
    status: str,
    crawled_pages: int | None = None,
    extracted: dict[str, Any] | None = None,
    proposal: dict[str, Any] | None = None,
    decision_id: uuid.UUID | None = None,
    error_message: str | None = None,
    job_id: int | None = None,
) -> None:
    row.status = status
    row.crawled_pages = crawled_pages
    row.extracted = extracted
    row.proposal = proposal
    row.decision_id = decision_id
    row.error_message = error_message
    if job_id is not None:
        row.job_id = job_id
    row.finished_at = utc_now()
    await session.flush()
