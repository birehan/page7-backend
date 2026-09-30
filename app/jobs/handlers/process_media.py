"""Process a media_assets row: checksum, EXIF strip, webp variants, video poster.

Re-entrant by construction — re-running after a crash re-derives the same
outputs and re-writes the same rows (architecture/12).
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

import structlog
from PIL import Image, ImageOps

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.features.media import repository
from app.features.media.service import source_hash
from app.integrations.storage import get_object_storage
from app.jobs.errors import TerminalError

log = structlog.get_logger(__name__)

_VARIANT_WIDTHS = (320, 1080)


async def handle(payload: dict[str, Any]) -> None:
    media_id_raw = payload.get("media_asset_id")
    if not isinstance(media_id_raw, str):
        raise TerminalError("media.process payload missing media_asset_id")
    media_id = uuid.UUID(media_id_raw)
    settings = get_settings()
    storage = get_object_storage(settings)
    factory = get_session_factory()

    async with factory() as session:
        asset = await repository.get_media_asset_by_id(session, media_id=media_id)
        if asset is None:
            # Soft-deleted or never existed — nothing to do (re-entrant).
            await session.commit()
            return
        try:
            body = await storage.get_object("public", asset.r2_key)
            checksum = hashlib.sha256(body).hexdigest()
            poster_key: str | None = None

            if asset.kind == "image":
                await _write_image_variants(
                    storage,
                    asset_r2_key=asset.r2_key,
                    body=body,
                    organization_id=asset.organization_id,
                    media_asset_id=asset.id,
                    session=session,
                    checksum=checksum,
                    focal_x=asset.focal_x,
                    focal_y=asset.focal_y,
                    template_params=asset.template_params,
                )
            elif asset.kind == "video":
                poster_key = await _write_video_poster(
                    storage,
                    asset_r2_key=asset.r2_key,
                    body=body,
                )
                if poster_key is not None:
                    poster_bytes = await storage.get_object("public", poster_key)
                    await _write_image_variants(
                        storage,
                        asset_r2_key=poster_key,
                        body=poster_bytes,
                        organization_id=asset.organization_id,
                        media_asset_id=asset.id,
                        session=session,
                        checksum=checksum,
                        focal_x=asset.focal_x,
                        focal_y=asset.focal_y,
                        template_params=None,
                        purpose_prefix="thumb",
                    )

            await repository.mark_asset_status(
                session,
                asset=asset,
                status="ready",
                checksum_sha256=checksum,
                poster_r2_key=poster_key,
            )
            await session.commit()
            log.info(
                "media_processing_ready",
                organization_id=str(asset.organization_id),
                brand_id=str(asset.brand_id),
                media_asset_id=str(asset.id),
            )
        except Exception:
            log.exception(
                "media_processing_failed",
                organization_id=str(asset.organization_id),
                brand_id=str(asset.brand_id),
                media_asset_id=str(asset.id),
            )
            await session.rollback()
            async with factory() as fail_session:
                fresh = await repository.get_media_asset_by_id(
                    fail_session, media_id=media_id
                )
                if fresh is not None:
                    await repository.mark_asset_status(
                        fail_session, asset=fresh, status="failed"
                    )
                    await fail_session.commit()
            # Job itself succeeds — status=failed is a business outcome, not a
            # runner failure (phase-05 Observability requirements).
            return


async def _write_image_variants(
    storage: Any,
    *,
    asset_r2_key: str,
    body: bytes,
    organization_id: uuid.UUID,
    media_asset_id: uuid.UUID,
    session: Any,
    checksum: str,
    focal_x: Any,
    focal_y: Any,
    template_params: dict[str, Any] | None,
    purpose_prefix: str = "thumb",
) -> None:
    base_prefix = asset_r2_key.rsplit("/original", 1)[0]
    if asset_r2_key.endswith("/original"):
        variant_prefix = f"{base_prefix}/variants"
    else:
        # Poster key already under .../poster.jpg — put variants beside it.
        variant_prefix = f"{asset_r2_key.rsplit('/', 1)[0]}/variants"

    with Image.open(io.BytesIO(body)) as opened:
        img = ImageOps.exif_transpose(opened)
        if img.mode not in {"RGB", "RGBA"}:
            img = img.convert("RGB")
        for width in _VARIANT_WIDTHS:
            ratio = width / img.width if img.width else 1
            height = max(1, int(img.height * ratio))
            resized = img.resize((width, height), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            resized.save(out, format="WEBP", quality=82)
            data = out.getvalue()
            aspect = "original"
            purpose = purpose_prefix
            key = f"{variant_prefix}/{purpose}_{aspect}_{width}.webp"
            await storage.put_object(
                "public", key, data, content_type="image/webp"
            )
            await repository.upsert_variant(
                session,
                organization_id=organization_id,
                media_asset_id=media_asset_id,
                purpose=purpose,
                aspect=aspect,
                platform=str(width),
                r2_key=key,
                content_type="image/webp",
                width=width,
                height=height,
                size_bytes=len(data),
                source_hash=source_hash(
                    checksum_sha256=checksum,
                    focal_x=focal_x,
                    focal_y=focal_y,
                    template_params=template_params,
                    aspect=aspect,
                ),
            )


async def _write_video_poster(storage: Any, *, asset_r2_key: str, body: bytes) -> str:
    poster_key = asset_r2_key.rsplit("/original", 1)[0] + "/variants/poster_original.jpg"
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "input.bin"
        dst = Path(tmp) / "poster.jpg"
        src.write_bytes(body)
        try:
            cmd = [
                "ffmpeg",  # noqa: S607
                "-y",
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(dst),
            ]
            subprocess.run(  # noqa: S603, ASYNC221
                cmd,
                check=True,
                capture_output=True,
                timeout=60,
            )
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            raise TerminalError(f"ffmpeg poster failed: {exc}") from exc
        await storage.put_object(
            "public", poster_key, dst.read_bytes(), content_type="image/jpeg"
        )
    return poster_key
