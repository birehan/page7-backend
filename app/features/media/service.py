from __future__ import annotations

import hashlib
import io
import json
import secrets
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.ids import new_uuid7
from app.core.time import utc_now
from app.features import audit, brands
from app.features.media import repository
from app.features.media.models import MediaAsset
from app.features.media.schemas import (
    CreateUploadUrlBody,
    CreateUploadUrlResponse,
    FocalPointIn,
    GenerationParamsOut,
    ImportStockBody,
    MediaAttributionOut,
    MediaLibraryItemOut,
    MediaLibraryPage,
    StockPhotoOut,
    StockSearchResponse,
    TemplateParamsOut,
    UpdateFocalPointBody,
    UpdateMediaAltBody,
)
from app.integrations.storage.ports import CopyObjectRequest, ObjectStorage, PresignUploadRequest
from app.jobs import queue as job_queue

logger = structlog.get_logger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_VIDEO_BYTES = 200 * 1024 * 1024
INTENT_TTL_HOURS = 24


def _is_image(content_type: str) -> bool:
    return content_type.startswith("image/")


def _is_video(content_type: str) -> bool:
    return content_type.startswith("video/")


def validate_declared(*, content_type: str, size: int) -> None:
    """First checkpoint — cheap, against the client's declared values."""
    if not (_is_image(content_type) or _is_video(content_type)):
        raise ApiError(
            "UNSUPPORTED_MEDIA_TYPE",
            "Only image/* and video/* uploads are accepted",
            status_code=415,
        )
    limit = MAX_IMAGE_BYTES if _is_image(content_type) else MAX_VIDEO_BYTES
    if size > limit:
        raise ApiError(
            "PAYLOAD_TOO_LARGE",
            f"Upload exceeds the {limit // (1024 * 1024)}MB limit",
            status_code=413,
            details={"limitBytes": limit},
        )


def validate_actual(*, content_type: str | None, size_bytes: int | None) -> tuple[str, int]:
    """Second checkpoint — against the real HEAD response."""
    if content_type is None or size_bytes is None:
        raise ApiError(
            "UNSUPPORTED_MEDIA_TYPE",
            "Uploaded object is missing content type or size",
            status_code=415,
        )
    validate_declared(content_type=content_type, size=size_bytes)
    return content_type, size_bytes


def _random_media_key(organization_id: UUID, brand_id: UUID) -> str:
    token = secrets.token_hex(16)  # 128-bit, ADR-0007
    return f"orgs/{organization_id}/brands/{brand_id}/media/{token}/original"


def source_hash(
    *,
    checksum_sha256: str | None,
    focal_x: Decimal | None,
    focal_y: Decimal | None,
    template_params: dict[str, Any] | None,
    aspect: str,
) -> str:
    """architecture/02 §4 / architecture/11 §9 — render cache key."""
    payload = {
        "checksum": checksum_sha256 or "",
        "focal_x": str(focal_x) if focal_x is not None else None,
        "focal_y": str(focal_y) if focal_y is not None else None,
        "template_params": template_params,
        "aspect": aspect,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def probe_image_dimensions(data: bytes) -> tuple[int, int]:
    """Pillow format detection failing on non-raster bytes (e.g. SVG renamed
    to .png) IS the SVG rejection — architecture/11 §11.
    """
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.verify()
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
    except Exception as exc:
        raise ApiError(
            "UNSUPPORTED_MEDIA_TYPE",
            "Uploaded bytes are not a supported raster image",
            status_code=415,
        ) from exc
    if width <= 0 or height <= 0:
        raise ApiError(
            "UNSUPPORTED_MEDIA_TYPE",
            "Image dimensions must be positive",
            status_code=415,
        )
    return width, height


def probe_video_metadata(path: Path) -> tuple[int, int, Decimal]:
    """Synchronous ffprobe at /complete time (architecture/11 §7)."""
    try:
        cmd = [
            "ffprobe",  # noqa: S607
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,duration",
            "-of",
            "json",
            str(path),
        ]
        result = subprocess.run(  # noqa: S603
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        raise ApiError(
            "UNSUPPORTED_MEDIA_TYPE",
            "Could not read video metadata",
            status_code=415,
        ) from exc
    payload = json.loads(result.stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise ApiError(
            "UNSUPPORTED_MEDIA_TYPE",
            "Video has no readable video stream",
            status_code=415,
        )
    stream = streams[0]
    width = int(stream.get("width") or 0)
    height = int(stream.get("height") or 0)
    duration = Decimal(str(stream.get("duration") or "0"))
    if width <= 0 or height <= 0:
        raise ApiError(
            "UNSUPPORTED_MEDIA_TYPE",
            "Video dimensions must be positive",
            status_code=415,
        )
    return width, height, duration


def asset_to_out(asset: MediaAsset, storage: ObjectStorage) -> MediaLibraryItemOut:
    focal = None
    if asset.focal_x is not None and asset.focal_y is not None:
        focal = FocalPointIn(x=float(asset.focal_x), y=float(asset.focal_y))
    attribution = None
    if asset.attribution is not None:
        attribution = MediaAttributionOut.model_validate(asset.attribution)
    template = None
    if asset.template_params is not None:
        template = TemplateParamsOut.model_validate(asset.template_params)
    generation = None
    if asset.generation is not None:
        generation = GenerationParamsOut.model_validate(asset.generation)
    return MediaLibraryItemOut(
        id=str(asset.id),
        brand_id=str(asset.brand_id),
        kind=asset.kind,
        url=storage.public_url(asset.r2_key),
        width=asset.width,
        height=asset.height,
        alt_ar=asset.alt_ar,
        alt_en=asset.alt_en,
        tags=list(asset.tags or []),
        created_at=asset.created_at,
        source=asset.source,
        attribution=attribution,
        template=template,
        generation=generation,
        focal_point=focal,
        duration_seconds=(
            float(asset.duration_seconds) if asset.duration_seconds is not None else None
        ),
        poster_url=(
            storage.public_url(asset.poster_r2_key) if asset.poster_r2_key else None
        ),
    )


async def _assert_brand_exists(
    session: AsyncSession, *, organization_id: UUID, brand_id: UUID
) -> None:
    brand = await brands.get_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )
    if brand is None:
        raise ApiError("NOT_FOUND", "Brand not found", status_code=404)


async def get_assets_by_ids(
    session: AsyncSession,
    *,
    organization_id: UUID,
    brand_id: UUID,
    asset_ids: list[UUID],
) -> list[MediaAsset]:
    """Brand-scoped asset lookup for post_media attachment (Phase 6)."""
    return list(
        await repository.get_media_assets_by_ids(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            asset_ids=asset_ids,
        )
    )


async def create_upload_intent(
    session: AsyncSession,
    *,
    organization_id: UUID,
    brand_id: UUID,
    actor_user_id: UUID,
    body: CreateUploadUrlBody,
    storage: ObjectStorage,
    public_bucket_name: str,
) -> CreateUploadUrlResponse:
    await _assert_brand_exists(
        session, organization_id=organization_id, brand_id=brand_id
    )
    validate_declared(content_type=body.content_type, size=body.size)

    intent_id = new_uuid7()
    tmp_key = f"tmp/{intent_id}/original"
    from datetime import timedelta

    expires_at = utc_now() + timedelta(hours=INTENT_TTL_HOURS)

    presign = await storage.presign_upload(
        PresignUploadRequest(
            bucket="public",
            key=tmp_key,
            content_type=body.content_type,
        )
    )
    await repository.insert_upload_intent(
        session,
        intent_id=intent_id,
        organization_id=organization_id,
        brand_id=brand_id,
        created_by=actor_user_id,
        r2_bucket=public_bucket_name,
        r2_key=tmp_key,
        original_filename=body.filename,
        content_type=body.content_type,
        declared_size=body.size,
        presign_headers=presign.required_headers,
        expires_at=expires_at,
    )
    logger.info(
        "media_upload_url_created",
        organization_id=str(organization_id),
        brand_id=str(brand_id),
        media_asset_id=str(intent_id),
    )
    return CreateUploadUrlResponse(
        upload_url=presign.upload_url,
        asset_id=str(intent_id),
        headers=presign.required_headers,
    )


async def complete_upload(
    session: AsyncSession,
    *,
    organization_id: UUID,
    brand_id: UUID,
    asset_id: UUID,
    actor_user_id: UUID,
    actor_name: str,
    storage: ObjectStorage,
    public_bucket_name: str,
) -> MediaLibraryItemOut:
    intent = await repository.get_upload_intent(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        intent_id=asset_id,
    )
    if intent is None or intent.completed_at is not None:
        raise ApiError("NOT_FOUND", "Upload intent not found", status_code=404)
    if intent.expires_at <= utc_now():
        raise ApiError("NOT_FOUND", "Upload intent has expired", status_code=404)

    head = await storage.head_object("public", intent.r2_key)
    if not head.exists:
        raise ApiError("NOT_FOUND", "Uploaded object not found in storage", status_code=404)
    content_type, size_bytes = validate_actual(
        content_type=head.content_type or intent.content_type,
        size_bytes=head.size_bytes,
    )

    duration: Decimal | None = None
    if _is_image(content_type):
        # Ranged S3 GET rather than a public HTTP GET — deliberate Phase 5
        # deviation from architecture/11 §7 (see phase doc amendment).
        header_bytes = await storage.get_object_range(
            "public", intent.r2_key, start=0, end=min(size_bytes - 1, 512 * 1024)
        )
        width, height = probe_image_dimensions(header_bytes)
        kind = "image"
    else:
        body = await storage.get_object("public", intent.r2_key)
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
            tmp.write(body)
            tmp_path = Path(tmp.name)
        try:
            width, height, duration = probe_video_metadata(tmp_path)
        finally:
            tmp_path.unlink(missing_ok=True)  # noqa: ASYNC240
        kind = "video"

    permanent_key = _random_media_key(organization_id, brand_id)
    await storage.copy_object(
        CopyObjectRequest(
            source=f"r2://{public_bucket_name}/{intent.r2_key}",
            dest_bucket="public",
            dest_key=permanent_key,
            content_type=content_type,
        )
    )
    await storage.delete_object("public", intent.r2_key)
    await repository.mark_intent_completed(session, intent=intent)

    asset = await repository.insert_media_asset(
        session,
        asset_id=intent.id,
        organization_id=organization_id,
        brand_id=brand_id,
        kind=kind,
        source="upload",
        status="processing",
        r2_bucket=public_bucket_name,
        r2_key=permanent_key,
        content_type=content_type,
        size_bytes=size_bytes,
        width=width,
        height=height,
        duration_seconds=duration,
        uploaded_by=actor_user_id,
        original_filename=intent.original_filename,
    )
    await job_queue.enqueue(
        session,
        queue="media",
        type="media.process",
        payload={"media_asset_id": str(asset.id)},
        unique_key=f"media.process:{asset.id}",
    )
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="media.uploaded",
        target_type="media_asset",
        target_id=asset.id,
    )
    logger.info(
        "media_upload_completed",
        organization_id=str(organization_id),
        brand_id=str(brand_id),
        media_asset_id=str(asset.id),
    )
    return asset_to_out(asset, storage)


async def list_media(
    session: AsyncSession,
    *,
    organization_id: UUID,
    brand_id: UUID,
    storage: ObjectStorage,
    cursor: str | None = None,
    kind: str | None = None,
    source: str | None = None,
    limit: int = repository.DEFAULT_PAGE_SIZE,
) -> MediaLibraryPage:
    await _assert_brand_exists(
        session, organization_id=organization_id, brand_id=brand_id
    )
    rows, next_cursor = await repository.list_media_page(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        cursor=cursor,
        kind=kind,
        source=source,
        limit=limit,
    )
    return MediaLibraryPage(
        items=[asset_to_out(row, storage) for row in rows],
        next_cursor=next_cursor,
    )


async def update_media_alt(
    session: AsyncSession,
    *,
    organization_id: UUID,
    brand_id: UUID,
    media_id: UUID,
    body: UpdateMediaAltBody,
    storage: ObjectStorage,
) -> MediaLibraryItemOut:
    asset = await repository.get_media_asset(
        session, organization_id=organization_id, brand_id=brand_id, media_id=media_id
    )
    if asset is None:
        raise ApiError("NOT_FOUND", "Media asset not found", status_code=404)
    asset = await repository.update_alt(
        session, asset=asset, alt_ar=body.alt_ar, alt_en=body.alt_en
    )
    return asset_to_out(asset, storage)


async def update_focal_point(
    session: AsyncSession,
    *,
    organization_id: UUID,
    brand_id: UUID,
    media_id: UUID,
    body: UpdateFocalPointBody,
    storage: ObjectStorage,
) -> MediaLibraryItemOut:
    asset = await repository.get_media_asset(
        session, organization_id=organization_id, brand_id=brand_id, media_id=media_id
    )
    if asset is None:
        raise ApiError("NOT_FOUND", "Media asset not found", status_code=404)
    asset = await repository.update_focal_point(
        session,
        asset=asset,
        focal_x=Decimal(str(body.focal_point.x)),
        focal_y=Decimal(str(body.focal_point.y)),
    )
    return asset_to_out(asset, storage)


async def delete_media(
    session: AsyncSession,
    *,
    organization_id: UUID,
    brand_id: UUID,
    media_id: UUID,
    actor_user_id: UUID,
    actor_name: str,
) -> None:
    """Soft-delete a media asset; 409 MEDIA_IN_USE when attached to a live post."""
    asset = await repository.get_media_asset(
        session, organization_id=organization_id, brand_id=brand_id, media_id=media_id
    )
    if asset is None:
        raise ApiError("NOT_FOUND", "Media asset not found", status_code=404)

    from app.features import posts as posts_feature

    if await posts_feature.media_in_use(session, media_asset_id=media_id):
        raise ApiError(
            "MEDIA_IN_USE",
            "Media is attached to a scheduled or publishing post",
            status_code=409,
        )

    await repository.soft_delete_media(session, asset=asset, deleted_by=actor_user_id)
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="media.deleted",
        target_type="media_asset",
        target_id=media_id,
    )


async def soft_delete_for_brand(
    session: AsyncSession, *, brand_id: UUID, deleted_by: UUID
) -> int:
    return await repository.soft_delete_brand_media(
        session, brand_id=brand_id, deleted_by=deleted_by
    )


async def search_stock(
    *,
    query: str,
    page: int,
    stock_provider: Any,
) -> StockSearchResponse:
    result = await stock_provider.search(query=query, page=page)
    return StockSearchResponse(
        items=[
            StockPhotoOut(
                id=item.id,
                thumb_url=item.thumb_url,
                full_url=item.full_url,
                width=item.width,
                height=item.height,
                description=item.description,
                attribution=MediaAttributionOut(
                    provider=item.attribution.provider,
                    author=item.attribution.author,
                    author_url=item.attribution.author_url,
                    source_url=item.attribution.source_url,
                    license=item.attribution.license,
                ),
            )
            for item in result.items
        ],
        next_page=result.next_page,
    )


async def import_stock_photo(
    session: AsyncSession,
    *,
    organization_id: UUID,
    brand_id: UUID,
    actor_user_id: UUID,
    actor_name: str,
    body: ImportStockBody,
    storage: ObjectStorage,
    public_bucket_name: str,
    fetch_bytes: Any,
) -> MediaLibraryItemOut:
    await _assert_brand_exists(
        session, organization_id=organization_id, brand_id=brand_id
    )
    raw = await fetch_bytes(body.full_url)
    width, height = probe_image_dimensions(raw)
    permanent_key = _random_media_key(organization_id, brand_id)
    await storage.put_object(
        "public",
        permanent_key,
        raw,
        content_type="image/jpeg",
    )
    asset_id = new_uuid7()
    attribution = {
        "provider": body.attribution.provider,
        "author": body.attribution.author,
        "authorUrl": body.attribution.author_url,
        "sourceUrl": body.attribution.source_url,
        "license": body.attribution.license,
    }
    attribution = {k: v for k, v in attribution.items() if v is not None}
    asset = await repository.insert_media_asset(
        session,
        asset_id=asset_id,
        organization_id=organization_id,
        brand_id=brand_id,
        kind="image",
        source="stock",
        status="processing",
        r2_bucket=public_bucket_name,
        r2_key=permanent_key,
        content_type="image/jpeg",
        size_bytes=len(raw),
        width=width,
        height=height,
        alt_ar=body.alt_ar,
        alt_en=body.alt_en,
        attribution=attribution,
        uploaded_by=actor_user_id,
        original_filename=f"stock-{body.stock_id}.jpg",
    )
    await job_queue.enqueue(
        session,
        queue="media",
        type="media.process",
        payload={"media_asset_id": str(asset.id)},
        unique_key=f"media.process:{asset.id}",
    )
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="media.stock_imported",
        target_type="media_asset",
        target_id=asset.id,
        meta={"stockId": body.stock_id},
    )
    return asset_to_out(asset, storage)
