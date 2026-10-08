"""Visuals service — generate (SSE-over-jobs), keep, render (Phase 11 / v2).

Callers: features/visuals/router.py. Wires visual brief rewrite, model resolve,
and logo composite flags into the ai.visuals_generate job.
User: implement production AI image gen plan.
"""

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
from app.features.visuals.model_resolve import resolve_model
from app.features.visuals.prompt_rewrite import (
    PROMPT_VERSION,
    assemble_variant_prompts,
    color_names_from_hex,
    rewrite_visual_brief,
)
from app.features.visuals.schemas import GenerateVisualsBody
from app.infrastructure.ratelimit.limiter import RateLimiter
from app.integrations.llm import get_llm_router
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
    """Legacy assembler kept for unit tests; production uses assemble_generation_prompt."""
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
    color_names = color_names_from_hex(brand_colors)
    count = body.count
    mode = "poster" if body.style == "poster" else "photo"

    # Logo compositing is post-process — do NOT pass logo as model reference
    # (keeps brand mark pixel-faithful). reference_media_id still conditions.
    reference_image_urls: list[str] = []
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

    logo_url = brand.logo_url if body.use_brand_logo and brand.logo_url else None

    router = get_llm_router(settings)
    brief = await rewrite_visual_brief(
        caption_or_prompt=body.prompt,
        style=body.style,
        aspect=body.aspect,
        brand_name=brand.name,
        industry=brand.industry,
        city=brand.city,
        brand_colors=brand_colors,
        router=router,
        use_llm=True,
    )

    # User checkbox wins over LLM overlay_hint — never skip a requested logo.
    overlay_hint = brief.overlay_hint
    if logo_url and overlay_hint == "none":
        overlay_hint = "logo_bottom_left"
    if logo_url and not overlay_hint.startswith("logo_"):
        overlay_hint = "logo_bottom_left"

    prompt_variants = assemble_variant_prompts(
        brief,
        count=count,
        style_prefix=style_cfg.prompt_prefix,
        style_suffix=style_cfg.prompt_suffix,
        industry=brand.industry,
        city=brand.city,
        headline=body.headline,
        brand_colors_named=color_names,
        use_brand_colors=body.use_brand_colors,
        saudi_policy=body.style == "poster",
        mode=mode,
    )
    augmented = prompt_variants[0]

    model_id, param_profile = resolve_model(
        style_cfg,
        style=body.style,
        quality=body.quality,
        headline=body.headline or brief.headline_suggestion,
        prompt=body.prompt,
    )

    key_seed = _inputs_hash(
        {
            "prompt": body.prompt,
            "style": body.style,
            "aspect": body.aspect,
            "count": count,
            "headline": body.headline,
            "useBrandLogo": body.use_brand_logo,
            "quality": body.quality,
            "promptVersion": PROMPT_VERSION,
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
            "quality": body.quality,
            "referenceMediaId": str(body.reference_media_id)
            if body.reference_media_id
            else None,
            "promptVersion": PROMPT_VERSION,
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

    generation = await repository.get_generation(session, generation_id=run.id)
    if generation is None:
        generation = await repository.create_generation(
            session,
            organization_id=organization_id,
            brand_id=body.brand_id,
            requested_by=user_id,
            model=model_id,
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
                "prompt_variants": prompt_variants,
                "negative_prompt": brief.negative_prompt or None,
                "model_id": model_id,
                "image_size": image_size,
                "style": body.style,
                "aspect": body.aspect,
                "count": count,
                "use_brand_colors": body.use_brand_colors,
                "brand_colors": brand_colors,
                "num_inference_steps": style_cfg.num_inference_steps,
                "guidance_scale": style_cfg.guidance_scale,
                "param_profile": param_profile,
                "reference_image_urls": reference_image_urls,
                "original_prompt": body.prompt,
                "seed": body.seed,
                "logo_url": logo_url,
                "force_logo": bool(logo_url),
                "overlay_hint": overlay_hint,
                "quality": body.quality,
                "prompt_version": PROMPT_VERSION,
                "target_width": image_size["width"],
                "target_height": image_size["height"],
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
