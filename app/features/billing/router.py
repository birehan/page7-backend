"""Billing HTTP routes — org-scoped subscription, invoices, AI usage."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.request_context import AccessContext, require_capability, require_membership
from app.db.session import get_db_session
from app.features.billing import service as billing
from app.features.billing.schemas import (
    AiUsageOut,
    InvoiceOut,
    SubscriptionOut,
    UpdatePlanBody,
)
from app.infrastructure.idempotency.dependency import (
    IdempotencyContext,
    replay_response,
    require_idempotency,
    serialize_model,
)

router = APIRouter(prefix="/orgs/{orgId}/billing", tags=["billing"])


@router.get(
    "/subscription",
    response_model=SubscriptionOut,
    response_model_exclude_none=True,
)
async def get_subscription(
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> SubscriptionOut:
    return await billing.get_subscription(db, organization_id=ctx.organization_id)


@router.get(
    "/invoices",
    response_model=list[InvoiceOut],
    response_model_exclude_none=True,
)
async def list_invoices(
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[InvoiceOut]:
    return await billing.list_invoices(db, organization_id=ctx.organization_id)


@router.patch(
    "/subscription",
    response_model=SubscriptionOut,
    response_model_exclude_none=True,
)
async def update_subscription(
    body: UpdatePlanBody,
    response: Response,
    ctx: Annotated[AccessContext, Depends(require_capability("billing.manage"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    idem: Annotated[
        IdempotencyContext, Depends(require_idempotency("billing.subscription"))
    ],
) -> SubscriptionOut | dict[str, object]:
    if idem.is_replay:
        return replay_response(idem, response)

    result = await billing.change_plan(
        db,
        organization_id=ctx.organization_id,
        plan=body.plan,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
        settings=settings,
    )
    await idem.complete(status_code=200, body=serialize_model(result))
    return result


@router.get(
    "/ai-usage",
    response_model=AiUsageOut,
    response_model_exclude_none=True,
)
async def get_ai_usage(
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    since: Annotated[datetime, Query()],
) -> AiUsageOut:
    return await billing.get_ai_usage(
        db, organization_id=ctx.organization_id, since=since
    )
