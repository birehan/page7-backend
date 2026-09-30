from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.pagination import decode_cursor, encode_cursor
from app.core.time import utc_now
from app.features.media.models import MediaAsset, MediaVariant, UploadIntent

DEFAULT_PAGE_SIZE = 30
MAX_PAGE_SIZE = 100


async def insert_upload_intent(
    session: AsyncSession,
    *,
    intent_id: uuid.UUID,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    created_by: uuid.UUID,
    r2_bucket: str,
    r2_key: str,
    original_filename: str,
    content_type: str,
    declared_size: int,
    presign_headers: dict[str, str] | None,
    expires_at: datetime,
) -> UploadIntent:
    row = UploadIntent(
        id=intent_id,
        organization_id=organization_id,
        brand_id=brand_id,
        created_by=created_by,
        r2_bucket=r2_bucket,
        r2_key=r2_key,
        original_filename=original_filename,
        content_type=content_type,
        declared_size=declared_size,
        presign_headers=presign_headers,
        expires_at=expires_at,
    )
    session.add(row)
    await session.flush()
    return row


async def get_upload_intent(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    intent_id: uuid.UUID,
) -> UploadIntent | None:
    stmt = select(UploadIntent).where(
        UploadIntent.id == intent_id,
        UploadIntent.organization_id == organization_id,
        UploadIntent.brand_id == brand_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def mark_intent_completed(
    session: AsyncSession, *, intent: UploadIntent
) -> UploadIntent:
    intent.completed_at = utc_now()
    await session.flush()
    return intent


async def insert_media_asset(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    kind: str,
    source: str,
    status: str,
    r2_bucket: str,
    r2_key: str,
    content_type: str,
    size_bytes: int,
    width: int,
    height: int,
    duration_seconds: Decimal | None = None,
    alt_ar: str = "",
    alt_en: str = "",
    tags: list[str] | None = None,
    attribution: dict[str, Any] | None = None,
    uploaded_by: uuid.UUID | None = None,
    original_filename: str | None = None,
    generation: dict[str, Any] | None = None,
    image_generation_output_id: uuid.UUID | None = None,
    template_params: dict[str, Any] | None = None,
    ai_decision_id: uuid.UUID | None = None,
) -> MediaAsset:
    row = MediaAsset(
        id=asset_id,
        organization_id=organization_id,
        brand_id=brand_id,
        kind=kind,
        source=source,
        status=status,
        r2_bucket=r2_bucket,
        r2_key=r2_key,
        content_type=content_type,
        size_bytes=size_bytes,
        width=width,
        height=height,
        duration_seconds=duration_seconds,
        alt_ar=alt_ar,
        alt_en=alt_en,
        tags=tags or [],
        attribution=attribution,
        uploaded_by=uploaded_by,
        original_filename=original_filename,
        generation=generation,
        image_generation_output_id=image_generation_output_id,
        ai_decision_id=ai_decision_id,
    )
    if template_params is not None:
        row.template_params = template_params
    session.add(row)
    await session.flush()
    return row


async def get_media_asset(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    media_id: uuid.UUID,
) -> MediaAsset | None:
    stmt = select(MediaAsset).where(
        MediaAsset.id == media_id,
        MediaAsset.organization_id == organization_id,
        MediaAsset.brand_id == brand_id,
        MediaAsset.deleted_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_media_page(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    cursor: str | None,
    kind: str | None = None,
    source: str | None = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> tuple[Sequence[MediaAsset], str | None]:
    limit = min(max(limit, 1), MAX_PAGE_SIZE)
    stmt = select(MediaAsset).where(
        MediaAsset.organization_id == organization_id,
        MediaAsset.brand_id == brand_id,
        MediaAsset.deleted_at.is_(None),
    )
    if kind is not None:
        stmt = stmt.where(MediaAsset.kind == kind)
    if source is not None:
        stmt = stmt.where(MediaAsset.source == source)
    if cursor is not None:
        parsed = decode_cursor(cursor)
        cursor_created_at = datetime.fromisoformat(parsed["created_at"])
        cursor_id = uuid.UUID(parsed["id"])
        stmt = stmt.where(
            tuple_(MediaAsset.created_at, MediaAsset.id)
            < (cursor_created_at, cursor_id)
        )
    stmt = stmt.order_by(MediaAsset.created_at.desc(), MediaAsset.id.desc()).limit(
        limit + 1
    )
    rows = (await session.execute(stmt)).scalars().all()
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more:
        last = page[-1]
        next_cursor = encode_cursor(
            {"created_at": last.created_at.isoformat(), "id": str(last.id)}
        )
    return page, next_cursor


async def update_alt(
    session: AsyncSession, *, asset: MediaAsset, alt_ar: str, alt_en: str
) -> MediaAsset:
    asset.alt_ar = alt_ar
    asset.alt_en = alt_en
    asset.updated_at = utc_now()
    await session.flush()
    return asset


async def update_focal_point(
    session: AsyncSession,
    *,
    asset: MediaAsset,
    focal_x: Decimal,
    focal_y: Decimal,
) -> MediaAsset:
    asset.focal_x = focal_x
    asset.focal_y = focal_y
    asset.updated_at = utc_now()
    await session.flush()
    return asset


async def soft_delete_media(
    session: AsyncSession, *, asset: MediaAsset, deleted_by: uuid.UUID
) -> None:
    asset.deleted_at = utc_now()
    asset.deleted_by = deleted_by
    await session.flush()


async def soft_delete_brand_media(
    session: AsyncSession, *, brand_id: uuid.UUID, deleted_by: uuid.UUID
) -> int:
    result = await session.execute(
        update(MediaAsset)
        .where(MediaAsset.brand_id == brand_id, MediaAsset.deleted_at.is_(None))
        .values(deleted_at=utc_now(), deleted_by=deleted_by)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def get_media_asset_by_id(
    session: AsyncSession, *, media_id: uuid.UUID
) -> MediaAsset | None:
    stmt = select(MediaAsset).where(
        MediaAsset.id == media_id, MediaAsset.deleted_at.is_(None)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_media_asset_by_generation_output_id(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    image_generation_output_id: uuid.UUID,
) -> MediaAsset | None:
    """Resolve the library row created by a prior keep of this generation output."""
    stmt = select(MediaAsset).where(
        MediaAsset.organization_id == organization_id,
        MediaAsset.brand_id == brand_id,
        MediaAsset.image_generation_output_id == image_generation_output_id,
        MediaAsset.deleted_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_media_assets_by_ids(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    asset_ids: Sequence[uuid.UUID],
) -> Sequence[MediaAsset]:
    if not asset_ids:
        return []
    stmt = select(MediaAsset).where(
        MediaAsset.id.in_(list(asset_ids)),
        MediaAsset.organization_id == organization_id,
        MediaAsset.brand_id == brand_id,
        MediaAsset.deleted_at.is_(None),
    )
    return (await session.execute(stmt)).scalars().all()


async def mark_asset_status(
    session: AsyncSession,
    *,
    asset: MediaAsset,
    status: str,
    checksum_sha256: str | None = None,
    poster_r2_key: str | None = None,
) -> MediaAsset:
    asset.status = status
    if checksum_sha256 is not None:
        asset.checksum_sha256 = checksum_sha256
    if poster_r2_key is not None:
        asset.poster_r2_key = poster_r2_key
    asset.updated_at = utc_now()
    await session.flush()
    return asset


async def upsert_variant(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    media_asset_id: uuid.UUID,
    purpose: str,
    aspect: str,
    platform: str | None,
    r2_key: str,
    content_type: str,
    width: int,
    height: int,
    size_bytes: int,
    source_hash: str,
) -> MediaVariant:
    stmt = select(MediaVariant).where(
        MediaVariant.media_asset_id == media_asset_id,
        MediaVariant.purpose == purpose,
        MediaVariant.aspect == aspect,
        MediaVariant.platform.is_(None)
        if platform is None
        else MediaVariant.platform == platform,
    )
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        existing.r2_key = r2_key
        existing.content_type = content_type
        existing.width = width
        existing.height = height
        existing.size_bytes = size_bytes
        existing.source_hash = source_hash
        await session.flush()
        return existing
    row = MediaVariant(
        organization_id=organization_id,
        media_asset_id=media_asset_id,
        purpose=purpose,
        aspect=aspect,
        platform=platform,
        r2_key=r2_key,
        content_type=content_type,
        width=width,
        height=height,
        size_bytes=size_bytes,
        source_hash=source_hash,
    )
    session.add(row)
    await session.flush()
    return row


async def list_expired_incomplete_intents(
    session: AsyncSession, *, now: datetime, limit: int = 500
) -> Sequence[UploadIntent]:
    stmt = (
        select(UploadIntent)
        .where(
            UploadIntent.completed_at.is_(None),
            UploadIntent.expires_at < now,
        )
        .order_by(UploadIntent.expires_at.asc())
        .limit(limit)
    )
    return (await session.execute(stmt)).scalars().all()


async def delete_upload_intent(session: AsyncSession, *, intent: UploadIntent) -> None:
    await session.delete(intent)
    await session.flush()
