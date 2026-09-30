"""HTTP surface for channels / Zernio (Phase 9).

Connect/reconnect/disconnect use `:channelId` (social_accounts row UUID),
matching the shipped frontend contract. oauth-url stays `:platform`.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

from app.core.config import Settings, get_settings
from app.core.request_context import (
    AccessContext,
    AuthenticatedUser,
    require_capability,
    require_membership,
    require_session,
    require_session_capability,
)
from app.core.time import utc_now
from app.db.session import get_db_session
from app.features.social_accounts import service
from app.features.social_accounts.schemas import (
    CapabilityMatrixOut,
    ChannelOAuthUrlBody,
    ChannelOAuthUrlOut,
    SocialAccountOut,
)

brand_router = APIRouter(
    prefix="/orgs/{orgId}/brands/{brandId}/channels", tags=["channels"]
)
capabilities_router = APIRouter(prefix="/channels", tags=["channels"])
callback_router = APIRouter(prefix="/integrations/zernio", tags=["integrations"])
webhook_router = APIRouter(prefix="/webhooks/zernio", tags=["webhooks"])

_DEFAULT_OAUTH_URL_BODY = ChannelOAuthUrlBody()


@brand_router.get("", response_model=list[SocialAccountOut], response_model_exclude_none=True)
async def list_channels(
    brandId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[SocialAccountOut]:
    return await service.list_channels(
        db, organization_id=ctx.organization_id, brand_id=brandId
    )


@brand_router.post(
    "/{platform}/oauth-url",
    response_model=ChannelOAuthUrlOut,
    response_model_exclude_none=True,
)
async def request_oauth_url(
    brandId: uuid.UUID,  # noqa: N803
    platform: str,
    ctx: Annotated[AccessContext, Depends(require_capability("brand.edit"))],
    user: Annotated[AuthenticatedUser, Depends(require_session)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    body: ChannelOAuthUrlBody = _DEFAULT_OAUTH_URL_BODY,
) -> ChannelOAuthUrlOut:
    return await service.request_oauth_url(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        platform=platform,
        user_id=ctx.user_id,
        session_id=user.session_id,
        settings=settings,
        return_path=body.return_path,
    )


@brand_router.post(
    "/{channelId}/connect",
    response_model=SocialAccountOut,
    response_model_exclude_none=True,
)
async def connect_channel(
    brandId: uuid.UUID,  # noqa: N803
    channelId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("brand.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SocialAccountOut:
    return await service.sync_from_provider(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        account_id=channelId,
        actor_user_id=ctx.user_id,
        kind="connect",
        settings=settings,
    )


@brand_router.post(
    "/{channelId}/reconnect",
    response_model=SocialAccountOut,
    response_model_exclude_none=True,
)
async def reconnect_channel(
    brandId: uuid.UUID,  # noqa: N803
    channelId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("brand.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SocialAccountOut:
    return await service.sync_from_provider(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        account_id=channelId,
        actor_user_id=ctx.user_id,
        kind="reconnect",
        settings=settings,
    )


@brand_router.post(
    "/{channelId}/disconnect",
    response_model=SocialAccountOut,
    response_model_exclude_none=True,
)
async def disconnect_channel(
    brandId: uuid.UUID,  # noqa: N803
    channelId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("brand.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> SocialAccountOut:
    return await service.disconnect_channel(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        account_id=channelId,
        actor_user_id=ctx.user_id,
        settings=settings,
    )


@capabilities_router.get(
    "/capabilities",
    response_model=CapabilityMatrixOut,
    response_model_exclude_none=True,
)
async def get_capabilities(
    _ctx: Annotated[AccessContext, Depends(require_session_capability("brand.edit"))],
) -> CapabilityMatrixOut:
    return service.capability_matrix()


@callback_router.get("/callback")
async def zernio_callback(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    state: Annotated[str | None, Query()] = None,
    connected: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    profile_id: Annotated[str | None, Query(alias="profileId")] = None,
    account_id: Annotated[str | None, Query(alias="accountId")] = None,
    username: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    # Missing state → soft-fail to the frontend (Zernio does not mint our CSRF
    # token; we embed it on redirect_url). Never return a JSON 422 to a browser.
    if not state:
        return RedirectResponse(
            url=service.build_callback_redirect(
                settings.social.callback_redirect_origin, error="invalid_state"
            ),
            status_code=302,
        )
    session_id = await _optional_session_id(request, db, settings)
    result = await service.handle_oauth_callback(
        db,
        state=state,
        session_id=session_id,
        connected=connected,
        error=error,
        profile_id=profile_id,
        account_id=account_id,
        username=username,
        settings=settings,
    )
    return RedirectResponse(url=result.url, status_code=302)


@webhook_router.post("/{alias}")
async def zernio_webhook(
    alias: str,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, bool]:
    body = await request.body()
    sig = request.headers.get("X-Zernio-Signature") or request.headers.get(
        "X-Late-Signature"
    )
    external_id = request.headers.get("X-Zernio-Event-Id")
    result = await service.ingest_webhook(
        db,
        alias=alias,
        body=body,
        signature=sig,
        external_event_id_header=external_id,
        settings=settings,
    )
    return result


async def _optional_session_id(
    request: Request,
    db: AsyncSession,
    settings: Settings,
) -> uuid.UUID | None:
    from sqlalchemy import text

    raw = request.cookies.get(settings.auth.session_cookie_name)
    if raw is None:
        return None
    token_hash = hashlib.sha256(raw.encode()).hexdigest()
    row = (
        await db.execute(
            text(
                "SELECT id, expires_at FROM sessions "
                "WHERE token_hash = :token_hash AND revoked_at IS NULL"
            ),
            {"token_hash": token_hash},
        )
    ).first()
    if row is None or row.expires_at < utc_now():
        return None
    return uuid.UUID(str(row.id))


# Convenience aggregate for main.py
router = brand_router
