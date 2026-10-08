"""Keep generated images and register client-rasterized templates (Phase 11)."""

from __future__ import annotations

import secrets
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.ids import new_uuid7
from app.features.media import repository as media_repo
from app.features.media.service import asset_to_out
from app.features.visuals import repository
from app.features.visuals.schemas import KeepVisualBody, RenderTemplateBody
from app.features.visuals.service import r2_key_from_public_url
from app.integrations.storage import get_object_storage
from app.integrations.storage.ports import CopyObjectRequest
from app.jobs import queue as job_queue


def _random_media_key(organization_id: uuid.UUID, brand_id: uuid.UUID) -> str:
    token = secrets.token_hex(16)
    return f"orgs/{organization_id}/brands/{brand_id}/media/{token}/original"


async def keep_visual(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    body: KeepVisualBody,
    settings: Settings,
) -> dict[str, Any]:
    storage = get_object_storage(settings)
    r2_key = r2_key_from_public_url(
        body.url, public_base_url=settings.storage.public_base_url
    )
    if r2_key is None or not r2_key.startswith("gen/pending/"):
        raise ApiError(
            "NOT_FOUND",
            "Generated image URL is not a pending generation",
            status_code=404,
        )

    output = await repository.get_output_by_r2_key(
        session, organization_id=organization_id, r2_key=r2_key
    )
    if output is None:
        raise ApiError("NOT_FOUND", "Generation output not found", status_code=404)

    generation = await repository.get_generation(
        session, generation_id=output.image_generation_id
    )
    if generation is None or generation.brand_id != body.brand_id:
        raise ApiError("NOT_FOUND", "Generation not found for brand", status_code=404)

    # Idempotent: a prior Use that saved the asset but didn't attach it to the post
    # (or a double-click) must return the existing library item, not 409.
    if output.kept_at is not None:
        existing = await media_repo.get_media_asset_by_generation_output_id(
            session,
            organization_id=organization_id,
            brand_id=body.brand_id,
            image_generation_output_id=output.id,
        )
        if existing is not None:
            return asset_to_out(existing, storage).model_dump(by_alias=True)
        raise ApiError("CONFLICT", "Image already kept", status_code=409)

    permanent_key = _random_media_key(organization_id, body.brand_id)
    public_bucket = settings.storage.public_bucket
    await storage.copy_object(
        CopyObjectRequest(
            source=f"r2://{public_bucket}/{r2_key}",
            dest_bucket="public",
            dest_key=permanent_key,
            content_type="image/png",
        )
    )

    head = await storage.head_object("public", permanent_key)
    size_bytes = head.size_bytes or 0
    asset_id = new_uuid7()
    asset = await media_repo.insert_media_asset(
        session,
        asset_id=asset_id,
        organization_id=organization_id,
        brand_id=body.brand_id,
        kind="image",
        source="generated",
        status="processing",
        r2_bucket=public_bucket,
        r2_key=permanent_key,
        content_type="image/png",
        size_bytes=size_bytes,
        width=output.width,
        height=output.height,
        alt_ar=body.alt_ar,
        alt_en=body.alt_en,
        uploaded_by=user_id,
        generation={"prompt": body.prompt, "style": body.style, "seed": output.seed},
        image_generation_output_id=output.id,
        ai_decision_id=generation.decision_id,
    )

    # Leave pending in place — clients may still hold gen/pending URLs briefly
    # after keep returns; deleting races the browser and breaks previews.
    await repository.mark_output_kept(session, output)

    await job_queue.enqueue(
        session,
        queue="media",
        type="media.process",
        payload={"media_asset_id": str(asset.id)},
        unique_key=f"media.process:{asset.id}",
        organization_id=organization_id,
    )
    await session.commit()
    return asset_to_out(asset, storage).model_dump(by_alias=True)


async def render_template(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    body: RenderTemplateBody,
    settings: Settings,
) -> dict[str, Any]:
    """Register a client-rasterized PNG (ADR-0006) — no server-side rendering."""
    _ = user_id
    storage = get_object_storage(settings)
    asset = await media_repo.get_media_asset(
        session,
        organization_id=organization_id,
        brand_id=body.brand_id,
        media_id=body.asset_id,
    )
    if asset is None or asset.deleted_at is not None:
        raise ApiError("NOT_FOUND", "Media asset not found", status_code=404)
    if asset.status != "ready":
        raise ApiError(
            "VALIDATION",
            "Asset must be ready before template registration",
            status_code=422,
        )

    # Rasterized PNG is a real image — keep kind=image so publish/schedule gates
    # accept it. source=template + template_params retain provenance for the
    # Templates tab (filtered by source, not kind).
    asset.kind = "image"
    asset.source = "template"
    asset.template_params = {
        "templateId": body.template_id,
        "headline": body.headline,
        "subline": body.subline,
        "aspect": body.aspect,
    }
    await session.flush()
    await session.commit()
    return asset_to_out(asset, storage).model_dump(by_alias=True)
