from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security.client_ip import client_ip
from app.db.session import get_db_session
from app.features.waitlist import service
from app.features.waitlist.schemas import WaitlistJoinIn, WaitlistJoinOut

router = APIRouter(prefix="/waitlist", tags=["waitlist"])


def _client_ip(request: Request) -> str | None:
    return client_ip(request)


@router.post("", response_model=WaitlistJoinOut, response_model_exclude_none=True)
async def join_waitlist(
    body: WaitlistJoinIn,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> WaitlistJoinOut:
    signup, already_joined = await service.join(
        db,
        email=body.email,
        business_name=body.business_name,
        city=body.city,
        phone=body.phone,
        locale=body.locale,
        source=body.source,
        ip=_client_ip(request),
    )
    return WaitlistJoinOut(
        id=signup.id,
        email=signup.email,
        already_joined=already_joined,
        created_at=signup.created_at,
    )
