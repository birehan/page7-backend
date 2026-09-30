from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import AccessContext, require_capability
from app.db.session import get_db_session
from app.features.review_links import service
from app.features.review_links.schemas import (
    CreateReviewLinkBody,
    CreateReviewLinkResponse,
)
from app.integrations.storage import get_object_storage
from app.integrations.storage.ports import ObjectStorage

router = APIRouter(
    prefix="/orgs/{orgId}/brands/{brandId}/posts/{postId}/review-links",
    tags=["review-links"],
)


@router.post(
    "",
    response_model=CreateReviewLinkResponse,
    response_model_exclude_none=True,
)
async def create_review_link(
    brandId: uuid.UUID,  # noqa: N803
    postId: uuid.UUID,  # noqa: N803
    body: CreateReviewLinkBody,
    ctx: Annotated[AccessContext, Depends(require_capability("review_link.create"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> CreateReviewLinkResponse:
    return await service.create_review_link(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        post_id=postId,
        body=body,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
        storage=storage,
    )
