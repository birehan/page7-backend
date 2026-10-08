"""ai.visuals_generate — fan-out Fal calls + brand finish + gen/pending copy.

Callers: job worker for type ai.visuals_generate.
User: implement logo-composite + aspect-pixels from production AI image gen plan.
"""

from __future__ import annotations

# Callers: ai.visuals_generate worker. Fixes: logo composite + variant diversity.
# User: logo not adding; variants almost exact; use Qwen default.

import asyncio
import random
import time
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal, cast

import httpx
import structlog
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.time import utc_now
from app.db.session import get_session_factory
from app.features import content_ai
from app.features.visuals import repository
from app.features.visuals.aspects import parse_image_size
from app.features.visuals.composite import apply_brand_finish
from app.features.visuals.prompt_rewrite import PROMPT_VERSION
from app.features.visuals.service import r2_key_from_public_url
from app.integrations.errors import ProviderTimeoutError, ProviderUnavailableError
from app.integrations.imagegen import get_image_generation_provider
from app.integrations.imagegen.ports import (
    ImageGenerationProvider,
    ImageGenerationRequest,
    ImageSize,
    ParamProfile,
    VisualAspect,
    VisualStyle,
)
from app.integrations.storage import get_object_storage
from app.integrations.storage.ports import CopyObjectRequest, ObjectStorage
from app.jobs.errors import TerminalError
from app.sse.bridge import emit, mark_run_finished, mark_run_running
from app.sse.models import Run

log = structlog.get_logger(__name__)

_VALID_PROFILES = frozenset({"flux", "qwen", "ideogram", "gpt_image"})


async def handle(payload: dict[str, Any]) -> None:
    try:
        run_id = uuid.UUID(str(payload["run_id"]))
        generation_id = uuid.UUID(str(payload["generation_id"]))
        organization_id = uuid.UUID(str(payload["organization_id"]))
        brand_id = uuid.UUID(str(payload["brand_id"]))
        user_id = uuid.UUID(str(payload["user_id"]))
        count = int(payload["count"])
        model_id = str(payload["model_id"])
        image_size: ImageSize = payload["image_size"]
        augmented_prompt = str(payload["augmented_prompt"])
        style = str(payload["style"])
        aspect = str(payload["aspect"])
        original_prompt = str(payload["original_prompt"])
    except (KeyError, ValueError, TypeError) as exc:
        raise TerminalError("VISUALS_GENERATE_BAD_PAYLOAD") from exc

    brand_colors_raw = payload.get("brand_colors") or []
    brand_colors = (
        [str(c) for c in brand_colors_raw] if isinstance(brand_colors_raw, list) else []
    )
    num_inference_steps = payload.get("num_inference_steps")
    guidance_scale = payload.get("guidance_scale")
    use_brand_colors = bool(payload.get("use_brand_colors", False))
    param_profile = str(payload.get("param_profile") or "flux")
    refs_raw = payload.get("reference_image_urls") or []
    reference_image_urls = (
        [str(u) for u in refs_raw] if isinstance(refs_raw, list) else []
    )
    seed_raw = payload.get("seed")
    # Always diversify variants — Fal with seed=None often returns near-duplicates.
    base_seed = int(seed_raw) if isinstance(seed_raw, int) else random.randint(1, 2_000_000_000)
    negative_prompt = payload.get("negative_prompt")
    logo_url = payload.get("logo_url")
    logo_url_str = str(logo_url) if isinstance(logo_url, str) and logo_url else None
    force_logo = bool(payload.get("force_logo")) or bool(logo_url_str)
    overlay_hint = str(payload.get("overlay_hint") or "logo_bottom_left")
    if force_logo and (overlay_hint == "none" or not overlay_hint.startswith("logo_")):
        overlay_hint = "logo_bottom_left"
    quality = str(payload.get("quality") or "standard")
    prompt_version = str(payload.get("prompt_version") or PROMPT_VERSION)
    variants_raw = payload.get("prompt_variants")
    prompt_variants: list[str] = (
        [str(p) for p in variants_raw]
        if isinstance(variants_raw, list) and variants_raw
        else [augmented_prompt]
    )
    try:
        target_width = int(payload.get("target_width") or parse_image_size(image_size)[0])
        target_height = int(
            payload.get("target_height") or parse_image_size(image_size)[1]
        )
    except (TypeError, ValueError):
        target_width, target_height = 1080, 1350

    settings = get_settings()
    provider = get_image_generation_provider(settings)
    storage = get_object_storage(settings)
    factory = get_session_factory()
    ttl_days = settings.imagegen.provider_url_ttl_days

    logo_bytes = await _load_logo_bytes(
        logo_url_str, storage=storage, settings=settings
    )
    if force_logo and logo_bytes is None:
        log.warning(
            "visuals_logo_missing_bytes",
            logo_url=logo_url_str,
            generation_id=str(generation_id),
        )

    async with factory() as session:
        run = (
            await session.execute(select(Run).where(Run.id == run_id))
        ).scalar_one_or_none()
        if run is None:
            await session.commit()
            return
        if run.status in {"succeeded", "partial", "failed"}:
            await session.commit()
            return
        await mark_run_running(session, run)
        generation = await repository.get_generation(
            session, generation_id=generation_id
        )
        if generation is not None:
            await repository.mark_generation_running(session, generation)
        await emit(
            session,
            run_id,
            {
                "type": "step",
                "step": "prompt",
                "status": "start",
                "runId": str(run_id),
            },
        )
        await emit(
            session, run_id, {"type": "step", "step": "prompt", "status": "done"}
        )
        if use_brand_colors or logo_bytes is not None:
            await emit(
                session, run_id, {"type": "step", "step": "brand", "status": "start"}
            )
            await emit(
                session, run_id, {"type": "step", "step": "brand", "status": "done"}
            )
        await emit(
            session, run_id, {"type": "step", "step": "generate", "status": "start"}
        )
        await session.commit()

    started = time.perf_counter()
    emit_lock = asyncio.Lock()

    async def _one(index: int) -> dict[str, Any]:
        prompt_i = prompt_variants[index % len(prompt_variants)]
        return await _generate_one(
            index=index,
            count=count,
            run_id=run_id,
            generation_id=generation_id,
            organization_id=organization_id,
            provider=provider,
            storage=storage,
            factory=factory,
            model_id=model_id,
            image_size=image_size,
            augmented_prompt=prompt_i,
            negative_prompt=str(negative_prompt) if negative_prompt else None,
            style=style,
            aspect=aspect,
            brand_colors=brand_colors,
            use_brand_colors=use_brand_colors,
            num_inference_steps=num_inference_steps,
            guidance_scale=guidance_scale,
            param_profile=param_profile,
            reference_image_urls=reference_image_urls,
            ttl_days=ttl_days,
            emit_lock=emit_lock,
            seed=base_seed + index * 9973,
            logo_bytes=logo_bytes,
            force_logo=force_logo,
            overlay_hint=overlay_hint,
            target_width=target_width,
            target_height=target_height,
            quality=quality,
        )

    outcomes = await asyncio.gather(*[_one(i) for i in range(count)])

    successes = sum(1 for o in outcomes if o["outcome"] == "success")
    failures = count - successes
    total_cost = sum(
        (o["cost"] for o in outcomes if o["outcome"] == "success"),
        Decimal("0"),
    )
    provider_request_ids = [
        o["provider_request_id"]
        for o in outcomes
        if o["outcome"] == "success" and o["provider_request_id"]
    ]
    last_error = next(
        (o["error"] for o in reversed(outcomes) if o["error"] is not None),
        None,
    )
    latency_ms = int((time.perf_counter() - started) * 1000)

    if successes == 0:
        status = "failed"
    elif failures > 0:
        status = "partial"
    else:
        status = "succeeded"

    async with factory() as session:
        generation = await repository.get_generation(
            session, generation_id=generation_id
        )
        if generation is None:
            raise TerminalError("VISUALS_GENERATE_MISSING_ROW")

        decision = await content_ai.insert_decision(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            actor_user_id=user_id,
            kind="image_generate",
            provider="fal",
            model=model_id,
            prompt_version=prompt_version,
            inputs_hash=str(generation_id),
            input_summary={
                "prompt": original_prompt,
                "augmentedPrompt": augmented_prompt,
                "style": style,
                "aspect": aspect,
                "count": count,
                "imageSize": image_size,
                "quality": quality,
                "enableSafetyChecker": True,
                "numInferenceSteps": num_inference_steps,
                "guidanceScale": guidance_scale,
            },
            output={
                "successes": successes,
                "failures": failures,
                "providerRequestIds": provider_request_ids,
            },
            target_type="image_generation",
            target_id=generation_id,
            status=status if status != "failed" else "failed",
            error_code=(
                last_error["code"] if last_error is not None and status == "failed" else None
            ),
            cost_usd=total_cost,
            latency_ms=latency_ms,
            provider_request_id=(
                provider_request_ids[0] if provider_request_ids else None
            ),
        )

        await repository.finalize_generation(
            session,
            generation,
            status=status,
            decision_id=decision.id,
            cost_usd=total_cost,
            latency_ms=latency_ms,
            error_code=(
                last_error["code"] if last_error is not None and status == "failed" else None
            ),
            provider_request_id=(
                provider_request_ids[0] if provider_request_ids else None
            ),
        )

        await emit(
            session,
            run_id,
            {"type": "step", "step": "generate", "status": "done"},
        )

        run = (await session.execute(select(Run).where(Run.id == run_id))).scalar_one()
        if status == "failed":
            error = last_error or {
                "code": "PROVIDER_UNAVAILABLE",
                "message": "All image generations failed",
            }
            await emit(session, run_id, {"type": "error", **error})
            await mark_run_finished(session, run, status="failed", error=error)
        else:
            await emit(
                session,
                run_id,
                {
                    "type": "done",
                    "decisionId": str(decision.id),
                    "runId": str(run_id),
                },
            )
            await mark_run_finished(
                session,
                run,
                status=status,
                result={
                    "decisionId": str(decision.id),
                    "successes": successes,
                    "failures": failures,
                    "costUsd": str(total_cost),
                },
            )
        await session.commit()


async def _fetch_bytes(url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content
    except Exception as exc:  # noqa: BLE001
        log.warning("visuals_fetch_bytes_failed", url=url, error=str(exc))
        return None


async def _load_logo_bytes(
    url: str | None,
    *,
    storage: ObjectStorage,
    settings: Settings,
) -> bytes | None:
    """Prefer object-storage get (works for local CDN URLs); fall back to HTTP."""
    if not url:
        return None
    key = r2_key_from_public_url(url, public_base_url=settings.storage.public_base_url)
    if key:
        try:
            return await storage.get_object("public", key)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "visuals_logo_storage_get_failed",
                key=key,
                error=str(exc),
            )
    return await _fetch_bytes(url)


def _corner_from_hint(hint: str) -> Literal[
    "bottom_left", "bottom_right", "top_left", "top_right"
]:
    mapping = {
        "logo_bottom_left": "bottom_left",
        "logo_bottom_right": "bottom_right",
        "logo_top_left": "top_left",
        "logo_top_right": "top_right",
    }
    return mapping.get(hint, "bottom_left")  # type: ignore[return-value]


async def _generate_one(
    *,
    index: int,
    count: int,
    run_id: uuid.UUID,
    generation_id: uuid.UUID,
    organization_id: uuid.UUID,
    provider: ImageGenerationProvider,
    storage: ObjectStorage,
    factory: Any,
    model_id: str,
    image_size: ImageSize,
    augmented_prompt: str,
    negative_prompt: str | None,
    style: str,
    aspect: str,
    brand_colors: list[str],
    use_brand_colors: bool,
    num_inference_steps: object,
    guidance_scale: object,
    param_profile: str,
    reference_image_urls: list[str],
    ttl_days: int,
    emit_lock: asyncio.Lock,
    seed: int | None = None,
    logo_bytes: bytes | None = None,
    force_logo: bool = False,
    overlay_hint: str = "logo_bottom_left",
    target_width: int = 1080,
    target_height: int = 1350,
    quality: str = "standard",
) -> dict[str, Any]:
    profile: ParamProfile = (
        cast(ParamProfile, param_profile)
        if param_profile in _VALID_PROFILES
        else "flux"
    )
    quality_tier = (
        cast(Any, quality) if quality in {"draft", "standard", "premium"} else "standard"
    )
    request = ImageGenerationRequest(
        prompt=augmented_prompt,
        style=cast(VisualStyle, style),
        aspect=cast(VisualAspect, aspect),
        brand_color_hint=brand_colors if use_brand_colors else [],
        model_id=model_id,
        image_size=image_size,
        param_profile=profile,
        reference_image_urls=reference_image_urls,
        enable_safety_checker=profile == "flux",
        num_inference_steps=(
            int(num_inference_steps) if isinstance(num_inference_steps, int) else None
        ),
        guidance_scale=(
            float(guidance_scale) if isinstance(guidance_scale, (int, float)) else None
        ),
        seed=seed,
        negative_prompt=negative_prompt,
        quality_tier=quality_tier,
    )

    try:
        result = await provider.generate_one(request)
        provider_error: dict[str, str] | None = None
    except ProviderTimeoutError as exc:
        result = None
        provider_error = {
            "code": "PROVIDER_UNAVAILABLE",
            "message": f"Image {index + 1} of {count} timed out: {exc}",
        }
    except (ProviderUnavailableError, Exception) as exc:
        result = None
        provider_error = {
            "code": "PROVIDER_UNAVAILABLE",
            "message": f"Image {index + 1} of {count} failed: {exc}",
        }

    async with emit_lock:
        if provider_error is not None:
            async with factory() as session:
                await emit(session, run_id, {"type": "error", **provider_error})
                await session.commit()
            return {
                "outcome": "error",
                "cost": Decimal("0"),
                "provider_request_id": None,
                "error": provider_error,
            }

        assert result is not None
        if result.image.flagged:
            error = {
                "code": "CONTENT_FILTERED",
                "message": f"Image {index + 1} of {count} filtered",
            }
            log.info(
                "ai_content_filtered",
                provider="fal",
                generation_id=str(generation_id),
                index=index,
            )
            async with factory() as session:
                await emit(session, run_id, {"type": "error", **error})
                await session.commit()
            return {
                "outcome": "filtered",
                "cost": Decimal("0"),
                "provider_request_id": result.usage.provider_request_id,
                "error": error,
            }

        pending_key = f"gen/pending/{generation_id}/{index}"
        expires_at = utc_now() + timedelta(days=ttl_days)

        raw_bytes: bytes | None = None
        if result.image.url.startswith("https://fal.media/files/fake/"):
            from app.integrations.imagegen.fakes import FAKE_PNG_BYTES

            raw_bytes = FAKE_PNG_BYTES
        else:
            raw_bytes = await _fetch_bytes(result.image.url)

        finished_bytes: bytes | None = None
        out_w, out_h = target_width, target_height
        if raw_bytes is not None:
            try:
                # force_logo (user checkbox) always wins over LLM overlay_hint=none.
                apply_logo = logo_bytes if (force_logo or overlay_hint != "none") else None
                if force_logo and apply_logo is None:
                    raise ValueError(
                        "Place logo was requested but brand logo bytes could not be loaded"
                    )
                finished_bytes = apply_brand_finish(
                    raw_bytes,
                    target_width=target_width,
                    target_height=target_height,
                    logo_bytes=apply_logo,
                    corner=_corner_from_hint(overlay_hint),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "visuals_brand_finish_failed",
                    error=str(exc),
                    generation_id=str(generation_id),
                    index=index,
                    force_logo=force_logo,
                )
                if force_logo:
                    error = {
                        "code": "LOGO_COMPOSITE_FAILED",
                        "message": (
                            f"Image {index + 1} of {count}: "
                            f"could not place brand logo ({exc})"
                        ),
                    }
                    async with factory() as session:
                        await emit(session, run_id, {"type": "error", **error})
                        await session.commit()
                    return {
                        "outcome": "error",
                        "cost": Decimal("0"),
                        "provider_request_id": result.usage.provider_request_id,
                        "error": error,
                    }
                finished_bytes = raw_bytes
                out_w, out_h = result.image.width, result.image.height

        async with factory() as session:
            output = await repository.insert_output(
                session,
                organization_id=organization_id,
                image_generation_id=generation_id,
                index=index,
                provider_url=result.image.url,
                provider_url_expires_at=expires_at,
                width=out_w,
                height=out_h,
                seed=result.image.seed,
            )
            try:
                if finished_bytes is not None:
                    await storage.put_object(
                        "public",
                        pending_key,
                        finished_bytes,
                        content_type="image/png",
                    )
                else:
                    await storage.copy_object(
                        CopyObjectRequest(
                            source=result.image.url,
                            dest_bucket="public",
                            dest_key=pending_key,
                            content_type="image/png",
                        )
                    )
                    out_w, out_h = result.image.width, result.image.height
            except Exception as exc:
                error = {
                    "code": "PROVIDER_UNAVAILABLE",
                    "message": (
                        f"Image {index + 1} of {count} "
                        f"copy into storage failed: {exc}"
                    ),
                }
                await emit(session, run_id, {"type": "error", **error})
                await session.commit()
                return {
                    "outcome": "error",
                    "cost": Decimal("0"),
                    "provider_request_id": result.usage.provider_request_id,
                    "error": error,
                }

            await repository.set_output_r2_key(session, output, r2_key=pending_key)
            our_url = storage.public_url(pending_key)
            await emit(
                session,
                run_id,
                {
                    "type": "image",
                    "index": index,
                    "total": count,
                    "url": our_url,
                    "width": out_w,
                    "height": out_h,
                },
            )
            await session.commit()

        return {
            "outcome": "success",
            "cost": result.usage.cost_usd,
            "provider_request_id": result.usage.provider_request_id,
            "error": None,
        }
