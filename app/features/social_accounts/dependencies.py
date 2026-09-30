"""FastAPI dependencies for social_accounts."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, Path
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.request_context import AccessContext, require_membership
from app.db.session import get_db_session
from app.features.social_accounts import repository
from app.features.social_accounts.models import SocialAccount


async def resolve_social_account(
    brandId: Annotated[uuid.UUID, Path(alias="brandId")],  # noqa: N803
    channelId: Annotated[uuid.UUID, Path(alias="channelId")],  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> SocialAccount:
    account = await repository.get_account(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        account_id=channelId,
    )
    if account is None:
        raise ApiError("NOT_FOUND", "Channel not found", status_code=404)
    return account
