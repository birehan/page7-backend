"""HTTP surface for brand research (architecture/08, Phase 8)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.core.config import Settings, get_settings
from app.core.request_context import AccessContext, require_session_capability
from app.db.session import get_db_session
from app.features.brand_research import service
from app.features.brand_research.schemas import GenerateBrandBody, RelearnBrandBody, RunOut

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/brand/relearn")
async def brand_relearn(
    body: RelearnBrandBody,
    ctx: Annotated[AccessContext, Depends(require_session_capability("brand.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> StreamingResponse:
    return await service.start_relearn(
        db,
        organization_id=ctx.organization_id,
        user_id=ctx.user_id,
        user_name=ctx.user_name,
        body=body,
        idempotency_key=idempotency_key,
        settings=settings,
    )


@router.post("/brand/generate")
async def brand_generate(
    body: GenerateBrandBody,
    ctx: Annotated[AccessContext, Depends(require_session_capability("brand.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> StreamingResponse:
    return await service.start_generate(
        db,
        organization_id=ctx.organization_id,
        user_id=ctx.user_id,
        user_name=ctx.user_name,
        body=body,
        idempotency_key=idempotency_key,
        settings=settings,
    )


@router.get("/runs/{runId}", response_model=RunOut)
async def get_ai_run(
    runId: uuid.UUID,
    ctx: Annotated[AccessContext, Depends(require_session_capability("brand.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> RunOut:
    return await service.get_run(db, organization_id=ctx.organization_id, run_id=runId)
