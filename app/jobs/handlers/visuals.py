"""ai.visuals_generate — fan-out Fal calls + immediate gen/pending copy."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal, cast

import structlog
from sqlalchemy import select

from app.core.config import get_settings
from app.core.time import utc_now
from app.db.session import get_session_factory
from app.features import content_ai
from app.features.visuals import repository
from app.integrations.errors import ProviderTimeoutError, ProviderUnavailableError
from app.integrations.imagegen import get_image_generation_provider
from app.integrations.imagegen.ports import (
    ImageGenerationProvider,
    ImageGenerationRequest,
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

_SlotOutcome = Literal["success", "filtered", "error"]


async def handle(payload: dict[str, Any]) -> None:
    try:
        run_id = uuid.UUID(str(payload["run_id"]))
        generation_id = uuid.UUID(str(payload["generation_id"]))
        organization_id = uuid.UUID(str(payload["organization_id"]))
        brand_id = uuid.UUID(str(payload["brand_id"]))
        user_id = uuid.UUID(str(payload["user_id"]))
        count = int(payload["count"])
        model_id = str(payload["model_id"])
        image_size = str(payload["image_size"])
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
    base_seed = int(seed_raw) if isinstance(seed_raw, int) else None

    settings = get_settings()
    provider = get_image_generation_provider(settings)
    storage = get_object_storage(settings)
    factory = get_session_factory()
    ttl_days = settings.imagegen.provider_url_ttl_days

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
        if use_brand_colors:
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
            augmented_prompt=augmented_prompt,
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
            # Deterministic per-index offset: reusing the exact same seed across every
            # image in a batch would make count>1 produce near-duplicate outputs
            # (architecture/09 review §6.3 "generate more like this"). Offsetting by
            # index keeps the batch varied while staying anchored to the requested seed.
            seed=(base_seed + index) if base_seed is not None else None,
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
            prompt_version="visuals.v1",
            inputs_hash=str(generation_id),
            input_summary={
                "prompt": original_prompt,
                "augmentedPrompt": augmented_prompt,
                "style": style,
                "aspect": aspect,
                "count": count,
                "imageSize": image_size,
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
    image_size: str,
    augmented_prompt: str,
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
) -> dict[str, Any]:
    profile: ParamProfile = "qwen" if param_profile == "qwen" else "flux"
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

    # Serialize DB writes / emit so concurrent fan-out cannot race on run_events.seq.
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

        async with factory() as session:
            output = await repository.insert_output(
                session,
                organization_id=organization_id,
                image_generation_id=generation_id,
                index=index,
                provider_url=result.image.url,
                provider_url_expires_at=expires_at,
                width=result.image.width,
                height=result.image.height,
                seed=result.image.seed,
            )
            try:
                if result.image.url.startswith("https://fal.media/files/fake/"):
                    from app.integrations.imagegen.fakes import FAKE_PNG_BYTES

                    await storage.put_object(
                        "public",
                        pending_key,
                        FAKE_PNG_BYTES,
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
                    "width": result.image.width,
                    "height": result.image.height,
                },
            )
            await session.commit()

        return {
            "outcome": "success",
            "cost": result.usage.cost_usd,
            "provider_request_id": result.usage.provider_request_id,
            "error": None,
        }
