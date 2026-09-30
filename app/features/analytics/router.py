"""Analytics HTTP routes — brand-scoped insight reports."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import AccessContext, require_membership
from app.db.session import get_db_session
from app.features.analytics import service as analytics
from app.features.analytics.schemas import InsightReportOut, InsightReportPage

router = APIRouter(
    prefix="/orgs/{orgId}/brands/{brandId}/analytics", tags=["analytics"]
)


@router.get(
    "/insight-reports",
    response_model=InsightReportPage,
    response_model_exclude_none=True,
)
async def list_insight_reports(
    brandId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
) -> InsightReportPage:
    return await analytics.list_insight_reports(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/insight-reports/latest",
    response_model=InsightReportOut,
    response_model_exclude_none=True,
)
async def get_latest_insight_report(
    brandId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> InsightReportOut:
    return await analytics.get_latest_insight_report(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
    )
