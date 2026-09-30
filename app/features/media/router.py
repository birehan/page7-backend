from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.request_context import AccessContext, require_capability, require_membership
from app.db.session import get_db_session
from app.features.media import service as media
from app.features.media.schemas import (
    CreateUploadUrlBody,
    CreateUploadUrlResponse,
    ImportStockBody,
    MediaLibraryItemOut,
    MediaLibraryPage,
    StockSearchResponse,
    UpdateFocalPointBody,
    UpdateMediaAltBody,
)
from app.integrations.stock import get_stock_provider
from app.integrations.stock.ports import StockProvider
from app.integrations.storage import get_object_storage
from app.integrations.storage.ports import ObjectStorage
from app.integrations.webfetch import safe_fetch

router = APIRouter(
    prefix="/orgs/{orgId}/brands/{brandId}/media", tags=["media"]
)


def _bucket_name(settings: Settings) -> str:
    return settings.storage.public_bucket


@router.get("", response_model=MediaLibraryPage, response_model_exclude_none=True)
async def list_media(
    brandId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    cursor: str | None = Query(default=None),
    kind: Literal["image", "video", "template"] | None = Query(default=None),
    source: Literal["upload", "stock", "generated", "template"] | None = Query(
        default=None
    ),
    limit: int = Query(default=30, ge=1, le=100),
) -> MediaLibraryPage:
    return await media.list_media(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        storage=storage,
        cursor=cursor,
        kind=kind,
        source=source,
        limit=limit,
    )


@router.post(
    "/upload-url",
    response_model=CreateUploadUrlResponse,
    response_model_exclude_none=True,
)
async def create_upload_url(
    brandId: uuid.UUID,  # noqa: N803
    body: CreateUploadUrlBody,
    ctx: Annotated[AccessContext, Depends(require_capability("post.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> CreateUploadUrlResponse:
    return await media.create_upload_intent(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        actor_user_id=ctx.user_id,
        body=body,
        storage=storage,
        public_bucket_name=_bucket_name(settings),
    )


@router.get(
    "/stock",
    response_model=StockSearchResponse,
    response_model_exclude_none=True,
)
async def search_stock(
    brandId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_membership)],
    stock: Annotated[StockProvider, Depends(get_stock_provider)],
    q: str = Query(default=""),
    page: int = Query(default=1, ge=1),
) -> StockSearchResponse:
    _ = brandId, ctx
    return await media.search_stock(query=q, page=page, stock_provider=stock)


@router.post(
    "/stock/import",
    response_model=MediaLibraryItemOut,
    response_model_exclude_none=True,
)
async def import_stock(
    brandId: uuid.UUID,  # noqa: N803
    body: ImportStockBody,
    ctx: Annotated[AccessContext, Depends(require_capability("post.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MediaLibraryItemOut:
    return await media.import_stock_photo(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
        body=body,
        storage=storage,
        public_bucket_name=_bucket_name(settings),
        fetch_bytes=safe_fetch,
    )


@router.post(
    "/{assetId}/complete",
    response_model=MediaLibraryItemOut,
    response_model_exclude_none=True,
)
async def complete_upload(
    brandId: uuid.UUID,  # noqa: N803
    assetId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> MediaLibraryItemOut:
    return await media.complete_upload(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        asset_id=assetId,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
        storage=storage,
        public_bucket_name=_bucket_name(settings),
    )


@router.patch(
    "/{mediaId}/focal-point",
    response_model=MediaLibraryItemOut,
    response_model_exclude_none=True,
)
async def update_focal_point(
    brandId: uuid.UUID,  # noqa: N803
    mediaId: uuid.UUID,  # noqa: N803
    body: UpdateFocalPointBody,
    ctx: Annotated[AccessContext, Depends(require_capability("post.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> MediaLibraryItemOut:
    return await media.update_focal_point(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        media_id=mediaId,
        body=body,
        storage=storage,
    )


@router.patch(
    "/{mediaId}",
    response_model=MediaLibraryItemOut,
    response_model_exclude_none=True,
)
async def update_media_alt(
    brandId: uuid.UUID,  # noqa: N803
    mediaId: uuid.UUID,  # noqa: N803
    body: UpdateMediaAltBody,
    ctx: Annotated[AccessContext, Depends(require_capability("post.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> MediaLibraryItemOut:
    return await media.update_media_alt(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        media_id=mediaId,
        body=body,
        storage=storage,
    )


@router.delete("/{mediaId}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_media(
    brandId: uuid.UUID,  # noqa: N803
    mediaId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("post.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    await media.delete_media(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        media_id=mediaId,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
