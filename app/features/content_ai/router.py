from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.core.config import Settings, get_settings
from app.core.request_context import (
    AccessContext,
    require_session_capability,
    require_session_membership,
)
from app.db.session import get_db_session
from app.features.content_ai import service
from app.features.content_ai.schemas import (
    AiFeedbackBody,
    AltTextBody,
    AltTextResponse,
    CommitPlanBody,
    GenerateCaptionsBody,
    GeneratePlanBody,
    RegenerateStrategyBody,
)
from app.integrations.llm import get_llm_router
from app.integrations.llm.router import LLMTaskRouter
from app.integrations.storage import get_object_storage
from app.integrations.storage.ports import ObjectStorage
from app.sse.in_request import sse_response

router = APIRouter(prefix="/ai", tags=["ai"])


@router.post("/captions")
async def captions(
    body: GenerateCaptionsBody,
    ctx: Annotated[AccessContext, Depends(require_session_capability("post.create"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    llm_router: Annotated[LLMTaskRouter, Depends(get_llm_router)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StreamingResponse:
    from app.features import billing as billing_feature

    await billing_feature.enforce_ai_credit_limit(
        db, organization_id=ctx.organization_id
    )
    return sse_response(
        service.stream_captions(
            db,
            organization_id=ctx.organization_id,
            user_id=ctx.user_id,
            user_name=ctx.user_name,
            body=body,
            router=llm_router,
            settings=settings,
        )
    )


@router.post("/plan")
async def plan(
    body: GeneratePlanBody,
    ctx: Annotated[AccessContext, Depends(require_session_capability("calendar.generate"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    llm_router: Annotated[LLMTaskRouter, Depends(get_llm_router)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StreamingResponse:
    from app.features import billing as billing_feature

    await billing_feature.enforce_ai_credit_limit(
        db, organization_id=ctx.organization_id
    )
    return sse_response(
        service.stream_plan(
            db,
            organization_id=ctx.organization_id,
            user_id=ctx.user_id,
            user_name=ctx.user_name,
            body=body,
            router=llm_router,
            settings=settings,
            storage=storage,
        )
    )


@router.post("/plan/commit")
async def plan_commit(
    body: CommitPlanBody,
    ctx: Annotated[AccessContext, Depends(require_session_capability("post.create"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> StreamingResponse:
    from app.features import billing as billing_feature

    await billing_feature.enforce_ai_credit_limit(
        db, organization_id=ctx.organization_id
    )
    return await service.commit_plan(
        db,
        organization_id=ctx.organization_id,
        user_id=ctx.user_id,
        user_name=ctx.user_name,
        body=body,
        idempotency_key=idempotency_key,
        settings=settings,
    )


@router.post("/strategy")
async def strategy(
    body: RegenerateStrategyBody,
    ctx: Annotated[AccessContext, Depends(require_session_capability("brand.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    llm_router: Annotated[LLMTaskRouter, Depends(get_llm_router)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> StreamingResponse:
    from app.features import billing as billing_feature

    await billing_feature.enforce_ai_credit_limit(
        db, organization_id=ctx.organization_id
    )
    return sse_response(
        service.stream_strategy(
            db,
            organization_id=ctx.organization_id,
            user_id=ctx.user_id,
            user_name=ctx.user_name,
            body=body,
            router=llm_router,
            settings=settings,
        )
    )


@router.post("/alt-text", response_model=AltTextResponse)
async def alt_text(
    body: AltTextBody,
    ctx: Annotated[AccessContext, Depends(require_session_capability("post.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    llm_router: Annotated[LLMTaskRouter, Depends(get_llm_router)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AltTextResponse:
    from app.features import billing as billing_feature

    await billing_feature.enforce_ai_credit_limit(
        db, organization_id=ctx.organization_id
    )
    return await service.generate_alt_text(
        db,
        organization_id=ctx.organization_id,
        user_id=ctx.user_id,
        user_name=ctx.user_name,
        body=body,
        router=llm_router,
        settings=settings,
        storage=storage,
    )


@router.post("/feedback", status_code=204)
async def feedback(
    body: AiFeedbackBody,
    ctx: Annotated[AccessContext, Depends(require_session_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    await service.record_feedback(
        db,
        organization_id=ctx.organization_id,
        user_id=ctx.user_id,
        body=body,
    )
    return Response(status_code=204)
