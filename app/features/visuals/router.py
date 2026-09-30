"""HTTP surface for visuals (architecture/09, Phase 11)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.core.config import Settings, get_settings
from app.core.request_context import AccessContext, require_session_capability
from app.db.session import get_db_session
from app.features.visuals import keep_render, service
from app.features.visuals.schemas import GenerateVisualsBody, KeepVisualBody, RenderTemplateBody

router = APIRouter(prefix="/visuals", tags=["visuals"])


@router.post("/generate")
async def generate_visuals(
    body: GenerateVisualsBody,
    ctx: Annotated[AccessContext, Depends(require_session_capability("post.edit"))],
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


@router.post("/keep")
async def keep_visual(
    body: KeepVisualBody,
    ctx: Annotated[AccessContext, Depends(require_session_capability("post.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    return await keep_render.keep_visual(
        db,
        organization_id=ctx.organization_id,
        user_id=ctx.user_id,
        body=body,
        settings=settings,
    )


@router.post("/render")
async def render_template(
    body: RenderTemplateBody,
    ctx: Annotated[AccessContext, Depends(require_session_capability("post.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    return await keep_render.render_template(
        db,
        organization_id=ctx.organization_id,
        user_id=ctx.user_id,
        body=body,
        settings=settings,
    )
