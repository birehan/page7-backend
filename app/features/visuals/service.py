"""Visuals service — generate (SSE-over-jobs), keep, render (Phase 11)."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.time import utc_now
from app.features import brands
from app.features.visuals import repository
from app.features.visuals.schemas import GenerateVisualsBody
from app.infrastructure.ratelimit.limiter import RateLimiter
from app.jobs import queue as job_queue
from app.sse.bridge import bridge_sse_response, create_run


def _inputs_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _brand_colors_from_out(guidelines: Any) -> list[str]:
    colors = getattr(guidelines, "colors", None)
    if colors is None and isinstance(guidelines, dict):
        colors = guidelines.get("colors")
    if not isinstance(colors, list):
        return []
    return [str(c) for c in colors if isinstance(c, str)]


def augment_prompt(
    *,
    prompt: str,
    style_prefix: str,
    style_suffix: str,
    brand_colors: list[str],
    use_brand_colors: bool,
    industry: str | None = None,
    city: str | None = None,
    headline: str | None = None,
    saudi_policy: bool = False,
) -> str:
    """Assemble the prompt sent to fal (architecture/09 §2 / §4 step order)."""
    parts: list[str] = []
    if style_prefix.strip():
        parts.append(style_prefix.strip())
    parts.append(prompt.strip())
    if headline and headline.strip():
        parts.append(f'render this headline text clearly: "{headline.strip()}"')
    if industry and industry.strip():
        parts.append(f"brand industry: {industry.strip()}")
    if city and city.strip():
        parts.append(f"city: {city.strip()}")
    if style_suffix.strip():
        parts.append(style_suffix.strip())
    if use_brand_colors and brand_colors:
        parts.append("brand palette: " + ", ".join(brand_colors))
    if saudi_policy:
        parts.append(
            "Saudi-market appropriate imagery: no alcohol, no pork, modest dress"
        )
    return ". ".join(parts)


async def _rate_limit(org_id: uuid.UUID, settings: Settings) -> None:
    limiter = RateLimiter()
    count = await limiter.increment(
        bucket=f"route:visuals_generate:{org_id}",
        now=utc_now(),
        window=timedelta(seconds=settings.imagegen.rate_limit_window_seconds),
    )
    if count > settings.imagegen.rate_limit_generate:
        raise ApiError("RATE_LIMIT", "Too many requests", status_code=429)


async def start_generate(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    user_name: str,
    body: GenerateVisualsBody,
    idempotency_key: str | None,
    settings: Settings,
) -> StreamingResponse:
    from app.features import billing as billing_feature

    await billing_feature.enforce_ai_credit_limit(
        session, organization_id=organization_id
    )
    await _rate_limit(organization_id, settings)

    brand = await brands.get_brand(
        session, organization_id=organization_id, brand_id=body.brand_id
    )
    if brand is None:
        raise ApiError("NOT_FOUND", "Brand not found", status_code=404)

    style_cfg = settings.imagegen.styles.get(body.style)
    if style_cfg is None:
        raise ApiError("VALIDATION", f"Unknown style: {body.style}", status_code=422)

    image_size = settings.imagegen.aspect_map.get(body.aspect)
    if image_size is None:
        raise ApiError("VALIDATION", f"Unknown aspect: {body.aspect}", status_code=422)

    brand_colors = _brand_colors_from_out(brand.guidelines)
    count = body.count

    reference_image_urls: list[str] = []
    if body.use_brand_logo and brand.logo_url:
        reference_image_urls.append(brand.logo_url)
    if body.reference_media_id is not None:
        from app.features.media import repository as media_repo
        from app.integrations.storage import get_object_storage

        asset = await media_repo.get_media_asset(
            session,
            organization_id=organization_id,
            brand_id=body.brand_id,
            media_id=body.reference_media_id,
        )
        if asset is None or asset.deleted_at is not None:
            raise ApiError("NOT_FOUND", "Reference media not found", status_code=404)
        storage = get_object_storage(settings)
        reference_image_urls.append(storage.public_url(asset.r2_key))
    reference_image_urls = reference_image_urls[:3]

    augmented = augment_prompt(
        prompt=body.prompt,
        style_prefix=style_cfg.prompt_prefix,
        style_suffix=style_cfg.prompt_suffix,
        brand_colors=brand_colors,
        use_brand_colors=body.use_brand_colors,
        industry=brand.industry,
        city=brand.city,
        headline=body.headline,
        saudi_policy=body.style == "poster",
    )

    key_seed = _inputs_hash(
        {
            "prompt": body.prompt,
            "style": body.style,
            "aspect": body.aspect,
            "count": count,
            "headline": body.headline,
            "useBrandLogo": body.use_brand_logo,
        }
    )[:16]
    key = idempotency_key or f"visuals_generate:{body.brand_id}:{key_seed}"
    payload_hash = _inputs_hash(
        {
            "brandId": str(body.brand_id),
            "prompt": body.prompt,
            "style": body.style,
            "aspect": body.aspect,
            "count": count,
            "useBrandColors": body.use_brand_colors,
            "useBrandLogo": body.use_brand_logo,
            "headline": body.headline,
            "referenceMediaId": str(body.reference_media_id)
            if body.reference_media_id
            else None,
        }
    )

    run = await create_run(
        session,
        organization_id=organization_id,
        brand_id=body.brand_id,
        kind="visuals_generate",
        idempotency_key=key,
        request_hash=payload_hash,
        created_by=user_id,
    )

    # Reuse run.id as the generation PK (same pattern as brand_research_runs)
    # so an idempotent re-POST finds the existing row.
    generation = await repository.get_generation(session, generation_id=run.id)
    if generation is None:
        generation = await repository.create_generation(
            session,
            organization_id=organization_id,
            brand_id=body.brand_id,
            requested_by=user_id,
            model=style_cfg.model_id,
            prompt=body.prompt,
            style=body.style,
            aspect=body.aspect,
            count=count,
            use_brand_colors=body.use_brand_colors,
            generation_id=run.id,
        )

    if run.status == "queued":
        await job_queue.enqueue(
            session,
            queue="ai",
            type="ai.visuals_generate",
            payload={
                "run_id": str(run.id),
                "generation_id": str(generation.id),
                "organization_id": str(organization_id),
                "brand_id": str(body.brand_id),
                "user_id": str(user_id),
                "user_name": user_name,
                "augmented_prompt": augmented,
                "model_id": style_cfg.model_id,
                "image_size": image_size,
                "style": body.style,
                "aspect": body.aspect,
                "count": count,
                "use_brand_colors": body.use_brand_colors,
                "brand_colors": brand_colors,
                "num_inference_steps": style_cfg.num_inference_steps,
                "guidance_scale": style_cfg.guidance_scale,
                "param_profile": style_cfg.param_profile,
                "reference_image_urls": reference_image_urls,
                "original_prompt": body.prompt,
                "seed": body.seed,
            },
            unique_key=f"visuals_generate:{run.id}",
            organization_id=organization_id,
        )
    await session.commit()
    return bridge_sse_response(run.id)


def r2_key_from_public_url(url: str, *, public_base_url: str) -> str | None:
    """Resolve a gen/pending public URL back to its R2 key."""
    base = public_base_url.rstrip("/")
    marker = "/_local-storage/public/"
    if url.startswith(base + marker):
        return url[len(base) + len(marker) :]
    if url.startswith(base + "/"):
        return url[len(base) + 1 :]
    parsed = urlparse(url)
    if parsed.path.startswith("/"):
        if marker in parsed.path:
            return parsed.path.split(marker, 1)[1]
        return parsed.path.lstrip("/")
    return None
