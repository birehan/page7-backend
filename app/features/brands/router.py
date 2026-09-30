from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.request_context import AccessContext, require_capability, require_membership
from app.db.session import get_db_session
from app.features.brands import service as brands
from app.features.brands.schemas import (
    BrandOut,
    CreateBrandBody,
    GenerateBrandBrainBody,
    IngestLogoBody,
    UpdateBrandBody,
)
from app.infrastructure.idempotency.dependency import (
    IdempotencyContext,
    replay_response,
    require_idempotency,
    serialize_model,
)
from app.integrations.storage import get_object_storage
from app.integrations.storage.ports import ObjectStorage

router = APIRouter(prefix="/orgs/{orgId}/brands", tags=["brands"])


@router.get("", response_model=list[BrandOut], response_model_exclude_none=True)
async def list_brands(
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[BrandOut]:
    return await brands.list_brands(db, organization_id=ctx.organization_id)


@router.post(
    "",
    response_model=BrandOut,
    status_code=status.HTTP_201_CREATED,
    response_model_exclude_none=True,
)
async def create_brand(
    body: CreateBrandBody,
    ctx: Annotated[AccessContext, Depends(require_capability("brand.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> BrandOut:
    return await brands.create_brand(
        db,
        organization_id=ctx.organization_id,
        body=body,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
    )


@router.get("/{brandId}", response_model=BrandOut, response_model_exclude_none=True)
async def get_brand(
    brandId: uuid.UUID,  # noqa: N803
    response: Response,
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> BrandOut:
    brand = await brands.get_brand(
        db, organization_id=ctx.organization_id, brand_id=brandId
    )
    if brand is None:
        raise ApiError("NOT_FOUND", "Brand not found", status_code=404)
    response.headers["ETag"] = f'"{brand.version}"'
    return brand


@router.patch("/{brandId}", response_model=BrandOut, response_model_exclude_none=True)
async def update_brand(
    brandId: uuid.UUID,  # noqa: N803
    body: UpdateBrandBody,
    response: Response,
    ctx: Annotated[AccessContext, Depends(require_capability("brand.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> BrandOut:
    brand = await brands.update_brand(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        body=body,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
        if_match=if_match,
    )
    response.headers["ETag"] = f'"{brand.version}"'
    return brand


@router.post(
    "/{brandId}/generate",
    response_model=BrandOut,
    response_model_exclude_none=True,
)
async def generate_brand(
    brandId: uuid.UUID,  # noqa: N803
    body: GenerateBrandBrainBody,
    response: Response,
    ctx: Annotated[AccessContext, Depends(require_capability("brand.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    idem: Annotated[
        IdempotencyContext, Depends(require_idempotency("brands.generate"))
    ],
) -> BrandOut | dict[str, object]:
    if idem.is_replay:
        return replay_response(idem, response)

    brand = await brands.generate_brand_defaults(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        body=body,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
    )
    await idem.complete(status_code=200, body=serialize_model(brand))
    response.headers["ETag"] = f'"{brand.version}"'
    return brand


@router.post(
    "/{brandId}/logo/ingest",
    response_model=BrandOut,
    response_model_exclude_none=True,
)
async def ingest_logo(
    brandId: uuid.UUID,  # noqa: N803
    body: IngestLogoBody,
    response: Response,
    ctx: Annotated[AccessContext, Depends(require_capability("brand.edit"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    storage: Annotated[ObjectStorage, Depends(get_object_storage)],
) -> BrandOut:
    brand = await brands.ingest_logo(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        body=body,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
        storage=storage,
    )
    response.headers["ETag"] = f'"{brand.version}"'
    return brand


@router.delete("/{brandId}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_brand(
    brandId: uuid.UUID,  # noqa: N803
    ctx: Annotated[AccessContext, Depends(require_capability("brand.delete"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    await brands.delete_brand(
        db,
        organization_id=ctx.organization_id,
        brand_id=brandId,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
    )
