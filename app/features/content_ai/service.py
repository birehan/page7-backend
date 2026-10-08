from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Literal
from urllib.parse import unquote

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.core.config import Settings
from app.core.errors import ApiError
from app.core.ids import new_uuid7
from app.core.time import riyadh_date_key, riyadh_datetime_to_utc, utc_now
from app.features import audit, brands, cultural_events, posts, strategy
from app.features.brands.schemas import BrandOut
from app.features.content_ai import repository
from app.features.content_ai.prompts import (
    alt_text as alt_text_prompt,
)
from app.features.content_ai.prompts import (
    caption_generation,
    caption_transform,
    plan_generation,
    strategy_generation,
)
from app.features.content_ai.schemas import (
    AiFeedbackBody,
    AltTextBody,
    AltTextResponse,
    CommitPlanBody,
    GenerateCaptionsBody,
    GeneratePlanBody,
    PlanDraftItemIn,
    RegenerateStrategyBody,
)
from app.features.posts.risk_engine import compute_risk
from app.features.posts.schemas import PostVariantIn
from app.infrastructure.ratelimit.limiter import RateLimiter
from app.integrations.errors import (
    ProviderContentFilteredError,
    ProviderPaymentRequiredError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.integrations.llm.ports import LLMMessage, StructuredGenerationRequest
from app.integrations.llm.router import LLMTaskRouter
from app.integrations.storage.ports import ObjectStorage
from app.jobs import queue as job_queue
from app.sse.bridge import bridge_sse_response, create_run
from app.sse.in_request import (
    KEEPALIVE_INTERVAL_SECONDS,
    MAX_STREAM_SECONDS,
    format_sse_comment,
    format_sse_event,
)

log = structlog.get_logger(__name__)

_TRANSFORM_INTENTS = frozenset(
    {
        "regenerate",
        "shorter",
        "longer",
        "add_cta",
        "formal",
        "casual",
        "translate_to_en",
        "translate_to_ar",
        "hashtags",
        "hook",
    }
)
_FIRST_COMMENT_PLATFORMS = frozenset({"instagram", "facebook"})


class LangVariantLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    caption: str
    hashtags: list[str] = Field(default_factory=list)


class CaptionVariantLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ar: LangVariantLLM
    en: LangVariantLLM
    first_comment: str | None = None


class CaptionOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    variants: list[CaptionVariantLLM]


def _normalize_caption_content(content: dict[str, Any]) -> dict[str, Any]:
    """Map legacy FakeLLM text_ar/text_en shape onto the canonical ar/en schema."""
    variants = content.get("variants")
    if not isinstance(variants, list):
        return content
    normalized: list[dict[str, Any]] = []
    for raw in variants:
        if not isinstance(raw, dict):
            continue
        if "ar" in raw and "en" in raw:
            normalized.append(raw)
            continue
        if "text_ar" in raw and "text_en" in raw:
            tags = _normalize_hashtags(raw.get("hashtags") or [])
            normalized.append(
                {
                    "ar": {"caption": raw["text_ar"], "hashtags": tags},
                    "en": {"caption": raw["text_en"], "hashtags": tags},
                    "first_comment": raw.get("first_comment"),
                }
            )
    if normalized:
        return {"variants": normalized}
    return content


def _normalize_strategy_content(content: dict[str, Any]) -> dict[str, Any]:
    goals_raw = content.get("goals")
    if not isinstance(goals_raw, list):
        return content
    goals: list[dict[str, Any]] = []
    for item in goals_raw:
        if isinstance(item, str):
            goals.append({"text": item, "progress": 0})
        elif isinstance(item, dict):
            goals.append(
                {
                    "text": item.get("text") or item.get("title") or "",
                    "progress": item.get("progress", 0),
                }
            )
    cadence = content.get("cadence") or {}
    if isinstance(cadence, dict):
        cadence = {
            k: float(v)
            for k, v in cadence.items()
            if isinstance(v, (int, float)) and k in {
                "instagram",
                "facebook",
                "tiktok",
                "snapchat",
                "whatsapp",
            }
        }
    return {"goals": goals, "cadence": cadence or {"instagram": 3}}


class PlanItemLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str | None = None
    platform: Literal["instagram", "facebook", "tiktok", "snapchat", "whatsapp"]
    day_offset: int = 0
    scheduled_time: str = "14:00"
    text_ar: str | None = None
    text_en: str | None = None
    hashtags_ar: list[str] = Field(default_factory=list)
    hashtags_en: list[str] = Field(default_factory=list)
    pillar_index: int = 0


class PlanOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    items: list[PlanItemLLM]


class StrategyGoalLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str
    progress: float = 0


class StrategyOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    goals: list[StrategyGoalLLM]
    cadence: dict[str, float]


class AltTextLLMOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    alt_ar: str
    alt_en: str


def _sse(event: dict[str, Any]) -> str:
    return format_sse_event("message", json.dumps(event, default=str))


def _inputs_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _rate_limit(
    org_id: uuid.UUID, bucket_suffix: str, limit: int, settings: Settings
) -> None:
    limiter = RateLimiter()
    count = await limiter.increment(
        bucket=f"route:ai_{bucket_suffix}:{org_id}",
        now=utc_now(),
        window=timedelta(seconds=settings.llm.rate_limit_window_seconds),
    )
    if count > limit:
        raise ApiError("RATE_LIMIT", "Too many requests", status_code=429)


def _tokenize(text: str) -> list[str]:
    parts = re.split(r"(\s+)", text)
    tokens: list[str] = []
    for i in range(0, len(parts), 2):
        tokens.append(parts[i] + (parts[i + 1] if i + 1 < len(parts) else ""))
    return [t for t in tokens if t]


def _provider_error_code(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ProviderContentFilteredError):
        return "CONTENT_FILTERED", "Content was filtered by the provider"
    # Payment/billing failures are ops concerns — never surface vendor or
    # credit details to end users (same copy as other transient provider issues).
    if isinstance(
        exc,
        (
            ProviderPaymentRequiredError,
            ProviderTimeoutError,
            ProviderRateLimitedError,
            ProviderUnavailableError,
        ),
    ):
        return "PROVIDER_UNAVAILABLE", "The AI provider is temporarily unavailable"
    return "INTERNAL", "An unexpected error occurred"


async def _run_structured_validated[T: BaseModel](
    router: LLMTaskRouter,
    task: str,
    request: StructuredGenerationRequest,
    model: type[T],
) -> tuple[T, Any]:
    """Call structured generation, validate once, retry once on ValidationError."""
    last_error: ValidationError | None = None
    messages = list(request.messages)
    for attempt in range(2):
        req = request.model_copy(update={"messages": messages})
        response = await router.run_structured(task, req)
        try:
            normalized = _normalize_caption_content(response.content)
            if task.startswith("caption"):
                return model.model_validate(normalized), response
            if task == "strategy_generation":
                return model.model_validate(_normalize_strategy_content(response.content)), response
            return model.model_validate(response.content), response
        except ValidationError as exc:
            last_error = exc
            if attempt == 0:
                messages = messages + [
                    LLMMessage(
                        role="user",
                        content=(
                            "Your previous JSON failed validation. "
                            f"Fix these errors and return valid JSON only: {exc.errors()}"
                        ),
                    )
                ]
    assert last_error is not None
    raise ApiError(
        "AI_OUTPUT_INVALID",
        "Model output failed validation",
        status_code=422,
    ) from last_error


async def _resolve_brand(
    session: AsyncSession, *, organization_id: uuid.UUID, brand_id: uuid.UUID
) -> BrandOut:
    brand = await brands.get_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )
    if brand is None:
        raise ApiError("NOT_FOUND", "Brand not found", status_code=404)
    return brand


async def _resolve_pillar(brand: BrandOut, pillar_id: uuid.UUID | None) -> Any:
    if pillar_id is None:
        return None
    pillar = next((p for p in brand.pillars if p.id == pillar_id), None)
    if pillar is None:
        raise ApiError("NOT_FOUND", "Pillar not found", status_code=404)
    return pillar


async def _resolve_cultural_event(
    session: AsyncSession, event_id: uuid.UUID | None
) -> Any:
    if event_id is None:
        return None
    event = await cultural_events.get_cultural_event(session, event_id=event_id)
    if event is None:
        raise ApiError("NOT_FOUND", "Cultural event not found", status_code=404)
    return event


def _banned_claims(brand: BrandOut) -> list[str]:
    return list(brand.guidelines.banned_claims or [])


def _r2_key_from_url(media_url: str, storage: ObjectStorage) -> str | None:
    """Reverse-map a public media URL to an r2_key, or None for external URLs."""
    sentinel = "__pgblank_probe__/key"
    probe = storage.public_url(sentinel)
    if sentinel not in probe:
        return None
    prefix = probe[: probe.index(sentinel)]
    if not media_url.startswith(prefix):
        return None
    return unquote(media_url[len(prefix) :])


def _normalize_hashtags(tags: list[str]) -> list[str]:
    """Sanitize hashtags straight from the LLM before they're persisted.

    Cheap/less-compliant models (observed live with gpt-4o-mini via
    OpenRouter) sometimes mangle the "#" prefix on non-first array entries —
    seen in real runs as both a literal "%23tag" and a literal "#%tag". Real
    provider quirks, not a page7/frontend bug. Normalize at the one place
    these get constructed so a raw artifact never reaches `posts.variants`
    and renders as a doubled/garbled hashtag.
    """
    normalized: list[str] = []
    for tag in tags:
        tag = tag.strip()
        if not tag:
            continue
        # Strip every leading "#"/"%23"/stray "%" combination down to the
        # bare tag text, then apply exactly one clean "#".
        stripped = re.sub(r"^(?:#|%23)+", "", tag)
        stripped = stripped.lstrip("%")
        if not stripped:
            continue
        normalized.append(f"#{stripped}")
    return normalized


async def _persist_decision(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    brand_version: int,
    actor_user_id: uuid.UUID,
    kind: str,
    provider: str,
    model: str,
    prompt_version: str,
    inputs_hash: str,
    input_summary: dict[str, Any],
    output: dict[str, Any] | None,
    status: Literal["succeeded", "failed", "partial"],
    target_type: str,
    target_id: uuid.UUID,
    parent_decision_id: uuid.UUID | None = None,
    error_code: str | None = None,
    prompt_tokens: int | None = None,
    completion_tokens: int | None = None,
    cost_usd: Decimal | None = None,
    latency_ms: int | None = None,
    provider_request_id: str | None = None,
    risk_score: Decimal | None = None,
) -> uuid.UUID:
    decision = await repository.insert_decision(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_user_id=actor_user_id,
        kind=kind,
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        inputs_hash=inputs_hash,
        input_summary=input_summary,
        output=output,
        status=status,
        target_type=target_type,
        target_id=target_id,
        brand_version=brand_version,
        parent_decision_id=parent_decision_id,
        error_code=error_code,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        latency_ms=latency_ms,
        provider_request_id=provider_request_id,
        risk_score=risk_score,
    )
    return decision.id


async def stream_captions(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    user_name: str,
    body: GenerateCaptionsBody,
    router: LLMTaskRouter,
    settings: Settings,
) -> AsyncIterator[str]:
    started = time.monotonic()
    last_keepalive = started

    async def maybe_keepalive() -> AsyncIterator[str]:
        nonlocal last_keepalive
        now = time.monotonic()
        if now - started > MAX_STREAM_SECONDS:
            yield _sse(
                {
                    "type": "error",
                    "code": "STREAM_TIMEOUT",
                    "message": "Stream timed out",
                }
            )
            return
        if now - last_keepalive >= KEEPALIVE_INTERVAL_SECONDS:
            yield format_sse_comment("keep-alive")
            last_keepalive = now

    try:
        await _rate_limit(
            organization_id, "captions", settings.llm.rate_limit_captions, settings
        )

        if body.intent in _TRANSFORM_INTENTS and body.seed is None:
            raise ApiError(
                "VALIDATION",
                "seed is required for transform intents",
                status_code=422,
            )

        brand = await _resolve_brand(
            session, organization_id=organization_id, brand_id=body.brand_id
        )
        pillar = await _resolve_pillar(brand, body.pillar_id)
        cultural_event = await _resolve_cultural_event(session, body.cultural_event_id)

        yield _sse({"type": "step", "step": "brand", "status": "start"})
        yield _sse({"type": "step", "step": "brand", "status": "done"})

        is_transform = body.intent != "generate"
        task = "caption_transform" if is_transform else "caption_generation"
        prompt_mod = caption_transform if is_transform else caption_generation

        brand_dict = brand.model_dump(mode="json", by_alias=True)
        pillar_dict = pillar.model_dump(mode="json", by_alias=True) if pillar else None
        event_dict = (
            cultural_event.model_dump(mode="json", by_alias=True) if cultural_event else None
        )
        seed_dict = body.seed.model_dump(mode="json", by_alias=True) if body.seed else None

        if is_transform:
            assert body.seed is not None
            messages = prompt_mod.build(
                brand=brand_dict,
                pillar=pillar_dict,
                cultural_event=event_dict,
                platforms=list(body.platforms),
                dialect=body.dialect,
                intent=body.intent,
                count=body.count,
                seed=seed_dict,
            )
        else:
            messages = prompt_mod.build(
                brand=brand_dict,
                pillar=pillar_dict,
                cultural_event=event_dict,
                platforms=list(body.platforms),
                dialect=body.dialect,
                count=body.count,
                seed=seed_dict,
            )

        request = StructuredGenerationRequest(
            messages=messages,
            output_schema=prompt_mod.OUTPUT_SCHEMA,
            schema_name=task,
        )

        t0 = time.monotonic()
        validated_variants: list[CaptionVariantLLM] = []
        llm_response = None
        failed_slots: list[int] = []

        try:
            output, llm_response = await _run_structured_validated(
                router, task, request, CaptionOutput
            )
            validated_variants = output.variants[: body.count]
        except ApiError as exc:
            if exc.code != "AI_OUTPUT_INVALID":
                raise
            yield _sse({"type": "error", "code": exc.code, "message": exc.message})
            await _persist_decision(
                session,
                organization_id=organization_id,
                brand_id=body.brand_id,
                brand_version=brand.version,
                actor_user_id=user_id,
                kind="captions",
                provider=settings.llm.tasks[task].provider,
                model=settings.llm.tasks[task].model,
                prompt_version=prompt_mod.PROMPT_VERSION,
                inputs_hash=_inputs_hash(body.model_dump(mode="json", by_alias=True)),
                input_summary={
                    "intent": body.intent,
                    "platforms": body.platforms,
                    "count": body.count,
                },
                output=None,
                status="failed",
                target_type="brand",
                target_id=body.brand_id,
                error_code="AI_OUTPUT_INVALID",
            )
            await session.commit()
            return

        start_index = body.variant_index if body.variant_index is not None else 0
        usage = llm_response.usage if llm_response else None
        max_risk = Decimal("0")
        output_variants: list[dict[str, Any]] = []

        for n, variant in enumerate(validated_variants):
            idx = start_index + n
            ar_text = variant.ar.caption
            en_text = variant.en.caption
            ar_tags = _normalize_hashtags(variant.ar.hashtags or [])
            en_tags = _normalize_hashtags(variant.en.hashtags or [])

            for lang, text in (("ar", ar_text), ("en", en_text)):
                yield _sse({"type": "step", "step": lang, "status": "start"})
                for token in _tokenize(text):
                    async for frame in maybe_keepalive():
                        yield frame
                    yield _sse(
                        {
                            "type": "token",
                            "variantIndex": idx,
                            "lang": lang,
                            "text": token,
                        }
                    )
                yield _sse({"type": "step", "step": lang, "status": "done"})

            yield _sse(
                {
                    "type": "hashtags",
                    "variantIndex": idx,
                    "lang": "ar",
                    "hashtags": ar_tags,
                }
            )
            yield _sse(
                {
                    "type": "hashtags",
                    "variantIndex": idx,
                    "lang": "en",
                    "hashtags": en_tags,
                }
            )

            ar_variant = {
                "lang": "ar",
                "dialect": body.dialect,
                "caption": ar_text,
                "hashtags": ar_tags,
            }
            en_variant = {
                "lang": "en",
                "caption": en_text,
                "hashtags": en_tags,
            }
            yield _sse(
                {"type": "variant_done", "variantIndex": idx, "variant": ar_variant}
            )
            yield _sse(
                {"type": "variant_done", "variantIndex": idx, "variant": en_variant}
            )

            if any(p in _FIRST_COMMENT_PLATFORMS for p in body.platforms):
                first_comment = variant.first_comment or " ".join(ar_tags[:5])
                yield _sse(
                    {
                        "type": "first_comment",
                        "variantIndex": idx,
                        "text": first_comment,
                    }
                )

            yield _sse({"type": "step", "step": "risk", "status": "start"})
            risk = compute_risk(
                variants=[ar_variant, en_variant],
                platforms=list(body.platforms),
                banned_claims=_banned_claims(brand),
            )
            max_risk = max(max_risk, Decimal(str(risk.score)))
            yield _sse(
                {
                    "type": "risk",
                    "variantIndex": idx,
                    "risk": risk.to_dict(),
                }
            )
            yield _sse({"type": "step", "step": "risk", "status": "done"})
            output_variants.append(
                {
                    "variantIndex": idx,
                    "ar": ar_variant,
                    "en": en_variant,
                    "riskScore": risk.score,
                }
            )

        status: Literal["succeeded", "failed", "partial"] = (
            "partial" if failed_slots else "succeeded"
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        decision_id = await _persist_decision(
            session,
            organization_id=organization_id,
            brand_id=body.brand_id,
            brand_version=brand.version,
            actor_user_id=user_id,
            kind="captions",
            provider=settings.llm.tasks[task].provider,
            model=settings.llm.tasks[task].model,
            prompt_version=prompt_mod.PROMPT_VERSION,
            inputs_hash=_inputs_hash(body.model_dump(mode="json", by_alias=True)),
            input_summary={
                "intent": body.intent,
                "platforms": body.platforms,
                "count": body.count,
                "pillarId": str(body.pillar_id) if body.pillar_id else None,
            },
            output={"variants": output_variants, "intent": body.intent},
            status=status,
            target_type="brand",
            target_id=body.brand_id,
            prompt_tokens=usage.prompt_tokens if usage else None,
            completion_tokens=usage.completion_tokens if usage else None,
            cost_usd=usage.cost_usd if usage else None,
            latency_ms=latency_ms,
            provider_request_id=usage.provider_request_id if usage else None,
            risk_score=max_risk if output_variants else None,
        )
        await audit.record(
            session,
            organization_id=organization_id,
            brand_id=body.brand_id,
            actor_kind="user",
            actor_ref=str(user_id),
            actor_name=user_name,
            actor_user_id=user_id,
            action="ai.captions_generated",
            target_type="ai_decision",
            target_id=decision_id,
            meta={"intent": body.intent, "count": len(output_variants)},
        )
        await session.commit()
        yield _sse({"type": "done", "decisionId": str(decision_id)})

    except ApiError as exc:
        yield _sse({"type": "error", "code": exc.code, "message": exc.message})
    except asyncio.CancelledError:
        return
    except (
        ProviderContentFilteredError,
        ProviderPaymentRequiredError,
        ProviderTimeoutError,
        ProviderRateLimitedError,
        ProviderUnavailableError,
    ) as exc:
        code, message = _provider_error_code(exc)
        yield _sse({"type": "error", "code": code, "message": message})
    except Exception:
        yield _sse(
            {
                "type": "error",
                "code": "INTERNAL",
                "message": "An unexpected error occurred",
            }
        )


def _event_covers_date(event: Any, date_key: str) -> bool:
    start = str(event.date.isoformat())
    end_val = event.end_date if event.end_date is not None else event.date
    end = str(end_val.isoformat())
    return bool(start <= date_key <= end)


_ARABIC_SCRIPT_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")
_LATIN_SCRIPT_RE = re.compile(r"[A-Za-z]")


def _script_score(text: str) -> tuple[int, int]:
    ar = len(_ARABIC_SCRIPT_RE.findall(text or ""))
    latin = len(_LATIN_SCRIPT_RE.findall(text or ""))
    return ar, latin


def _normalize_plan_language_slots(
    text_ar: str, text_en: str, hashtags_ar: list[str], hashtags_en: list[str]
) -> tuple[str, str, list[str], list[str]]:
    """If the model clearly swapped Arabic/English slots, unswap them."""
    ar_ar, ar_latin = _script_score(text_ar)
    en_ar, en_latin = _script_score(text_en)
    # Require clear majorities to avoid false swaps on mixed brand/place names.
    ar_looks_english = ar_latin >= 12 and ar_ar == 0
    en_looks_arabic = en_ar >= 12 and en_ar >= en_latin
    if not (ar_looks_english and en_looks_arabic):
        return text_ar, text_en, hashtags_ar, hashtags_en
    tag_ar_ar, tag_ar_latin = _script_score(" ".join(hashtags_ar))
    tag_en_ar, tag_en_latin = _script_score(" ".join(hashtags_en))
    tags_swapped = tag_ar_latin > tag_ar_ar and tag_en_ar > tag_en_latin
    if tags_swapped:
        return text_en, text_ar, hashtags_en, hashtags_ar
    return text_en, text_ar, hashtags_ar, hashtags_en


def _plan_item_from_llm(
    raw: PlanItemLLM,
    *,
    brand: BrandOut,
    from_date: date,
    index: int,
    cultural_events_list: list[Any],
) -> PlanDraftItemIn:
    pillars = brand.pillars or []
    pillar = pillars[raw.pillar_index % len(pillars)] if pillars else None
    if pillar is None:
        raise ApiError("VALIDATION", "Brand has no content pillars", status_code=422)

    day = from_date + timedelta(days=raw.day_offset)
    date_key = day.isoformat()
    scheduled_at = riyadh_datetime_to_utc(date_key, raw.scheduled_time)

    event_id: uuid.UUID | None = None
    for event in cultural_events_list:
        if event.enabled and _event_covers_date(event, date_key):
            event_id = event.id
            break

    dialect = brand.guidelines.dialect
    ar_caption = raw.text_ar or raw.title or f"{brand.name} — {raw.platform}"
    en_caption = raw.text_en or raw.title or f"{brand.name} — {raw.platform}"
    ar_tags = _normalize_hashtags(raw.hashtags_ar) or ["#السعودية"]
    en_tags = _normalize_hashtags(raw.hashtags_en) or ["#SaudiArabia"]
    ar_caption, en_caption, ar_tags, en_tags = _normalize_plan_language_slots(
        ar_caption, en_caption, ar_tags, en_tags
    )

    return PlanDraftItemIn(
        id=new_uuid7(),
        date_key=date_key,
        platform=raw.platform,
        scheduled_at=scheduled_at,
        pillar_id=pillar.id,
        cultural_event_id=event_id,
        variants=[
            PostVariantIn(
                lang="ar",
                dialect=dialect,
                caption=ar_caption,
                hashtags=ar_tags,
            ),
            PostVariantIn(lang="en", caption=en_caption, hashtags=en_tags),
        ],
    )


async def stream_plan(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    user_name: str,
    body: GeneratePlanBody,
    router: LLMTaskRouter,
    settings: Settings,
    storage: ObjectStorage,
) -> AsyncIterator[str]:
    started = time.monotonic()
    last_keepalive = started

    try:
        await _rate_limit(
            organization_id, "plan", settings.llm.rate_limit_plan, settings
        )
        brand = await _resolve_brand(
            session, organization_id=organization_id, brand_id=body.brand_id
        )

        yield _sse({"type": "step", "step": "calendar", "status": "start"})
        yield _sse({"type": "step", "step": "calendar", "status": "done"})

        scheduled_from = riyadh_datetime_to_utc(body.from_.isoformat(), "00:00")
        scheduled_to = riyadh_datetime_to_utc(body.to.isoformat(), "23:59")
        existing = await posts.list_posts(
            session,
            organization_id=organization_id,
            brand_id=body.brand_id,
            storage=storage,
            scheduled_from=scheduled_from,
            scheduled_to=scheduled_to,
        )
        existing_summary = ", ".join(
            f"{p.platform}@{riyadh_date_key(p.scheduled_at)}" for p in existing
        ) or "none"

        events = await cultural_events.list_cultural_events(
            session, organization_id=organization_id
        )
        messages = plan_generation.build(
            brand=brand.model_dump(mode="json", by_alias=True),
            from_date=body.from_,
            to_date=body.to,
            cadence=body.cadence,
            only_gaps=body.only_gaps,
            cultural_events=[e.model_dump(mode="json", by_alias=True) for e in events],
            existing_summary=existing_summary,
            target_count=body.target_count,
        )
        request = StructuredGenerationRequest(
            messages=messages,
            output_schema=plan_generation.OUTPUT_SCHEMA,
            schema_name="plan_generation",
        )

        yield _sse({"type": "step", "step": "plan", "status": "start"})
        t0 = time.monotonic()
        try:
            output, llm_response = await _run_structured_validated(
                router, "plan_generation", request, PlanOutput
            )
        except ApiError as exc:
            yield _sse({"type": "error", "code": exc.code, "message": exc.message})
            await session.commit()
            return

        span_days = (body.to - body.from_).days
        max_offset = max(span_days, 0)
        raw_items = [
            raw
            for raw in output.items
            if 0 <= raw.day_offset <= max_offset
        ]
        raw_items.sort(key=lambda raw: (raw.day_offset, raw.platform, raw.scheduled_time))
        if body.target_count is not None:
            raw_items = raw_items[: body.target_count]

        items: list[PlanDraftItemIn] = []
        for i, raw in enumerate(raw_items):
            if body.only_gaps:
                day_key = (body.from_ + timedelta(days=raw.day_offset)).isoformat()
                has_post = any(
                    p.platform == raw.platform and riyadh_date_key(p.scheduled_at) == day_key
                    for p in existing
                )
                if has_post:
                    continue
            items.append(
                _plan_item_from_llm(
                    raw,
                    brand=brand,
                    from_date=body.from_,
                    index=i,
                    cultural_events_list=events,
                )
            )

        total = len(items)
        serialized_items: list[dict[str, Any]] = []
        for i, item in enumerate(items):
            if time.monotonic() - started > MAX_STREAM_SECONDS:
                yield _sse(
                    {
                        "type": "error",
                        "code": "STREAM_TIMEOUT",
                        "message": "Stream timed out",
                    }
                )
                return
            if time.monotonic() - last_keepalive >= KEEPALIVE_INTERVAL_SECONDS:
                yield format_sse_comment("keep-alive")
                last_keepalive = time.monotonic()

            payload = item.model_dump(mode="json", by_alias=True, exclude_none=True)
            serialized_items.append(payload)
            yield _sse(
                {
                    "type": "plan_item",
                    "item": payload,
                    "index": i,
                    "total": total,
                }
            )

        yield _sse({"type": "step", "step": "plan", "status": "done"})

        usage = llm_response.usage
        task_cfg = settings.llm.tasks["plan_generation"]
        decision_id = await _persist_decision(
            session,
            organization_id=organization_id,
            brand_id=body.brand_id,
            brand_version=brand.version,
            actor_user_id=user_id,
            kind="plan",
            provider=task_cfg.provider,
            model=task_cfg.model,
            prompt_version=plan_generation.PROMPT_VERSION,
            inputs_hash=_inputs_hash(body.model_dump(mode="json", by_alias=True)),
            input_summary={
                "from": body.from_.isoformat(),
                "to": body.to.isoformat(),
                "onlyGaps": body.only_gaps,
            },
            output={"items": serialized_items},
            status="succeeded",
            target_type="brand",
            target_id=body.brand_id,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
            latency_ms=int((time.monotonic() - t0) * 1000),
            provider_request_id=usage.provider_request_id,
        )
        await audit.record(
            session,
            organization_id=organization_id,
            brand_id=body.brand_id,
            actor_kind="user",
            actor_ref=str(user_id),
            actor_name=user_name,
            actor_user_id=user_id,
            action="ai.plan_generated",
            target_type="ai_decision",
            target_id=decision_id,
            meta={"itemCount": total},
        )
        await session.commit()
        yield _sse({"type": "done", "decisionId": str(decision_id)})

    except ApiError as exc:
        yield _sse({"type": "error", "code": exc.code, "message": exc.message})
    except asyncio.CancelledError:
        return
    except (
        ProviderContentFilteredError,
        ProviderPaymentRequiredError,
        ProviderTimeoutError,
        ProviderRateLimitedError,
        ProviderUnavailableError,
    ) as exc:
        code, message = _provider_error_code(exc)
        yield _sse({"type": "error", "code": code, "message": message})
    except Exception:
        log.exception("stream_plan_failed")
        yield _sse(
            {
                "type": "error",
                "code": "INTERNAL",
                "message": "An unexpected error occurred",
            }
        )


async def commit_plan(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    user_name: str,
    body: CommitPlanBody,
    idempotency_key: str | None,
    settings: Settings,
) -> StreamingResponse:
    await _rate_limit(
        organization_id,
        "plan_commit",
        settings.llm.rate_limit_plan,
        settings,
    )

    await _resolve_brand(
        session, organization_id=organization_id, brand_id=body.brand_id
    )

    key = idempotency_key or f"plan-commit:{body.decision_id or body.items[0].id}"
    payload_hash = _inputs_hash(body.model_dump(mode="json", by_alias=True))

    parent_id: uuid.UUID | None = None
    if body.decision_id:
        try:
            parent_id = uuid.UUID(body.decision_id)
        except ValueError as exc:
            raise ApiError("VALIDATION", "Invalid decision id", status_code=422) from exc

    run = await create_run(
        session,
        organization_id=organization_id,
        brand_id=body.brand_id,
        kind="plan_commit",
        idempotency_key=key,
        request_hash=payload_hash,
        created_by=user_id,
    )

    items_payload = [item.model_dump(mode="json", by_alias=True) for item in body.items]
    await job_queue.enqueue(
        session,
        queue="ai",
        type="ai.plan_commit",
        payload={
            "run_id": str(run.id),
            "organization_id": str(organization_id),
            "brand_id": str(body.brand_id),
            "user_id": str(user_id),
            "user_name": user_name,
            "parent_decision_id": str(parent_id) if parent_id else None,
            "items": items_payload,
        },
        unique_key=f"plan_commit:{run.id}",
        organization_id=organization_id,
    )
    await session.commit()
    return bridge_sse_response(run.id)


async def stream_strategy(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    user_name: str,
    body: RegenerateStrategyBody,
    router: LLMTaskRouter,
    settings: Settings,
) -> AsyncIterator[str]:
    try:
        await _rate_limit(
            organization_id, "strategy", settings.llm.rate_limit_strategy, settings
        )
        brand = await _resolve_brand(
            session, organization_id=organization_id, brand_id=body.brand_id
        )
        current = await strategy.get_or_create_strategy(
            session, organization_id=organization_id, brand_id=body.brand_id
        )

        yield _sse({"type": "step", "step": "brand", "status": "start"})
        yield _sse({"type": "step", "step": "brand", "status": "done"})

        messages = strategy_generation.build(
            brand=brand.model_dump(mode="json", by_alias=True),
            current=current,
        )
        request = StructuredGenerationRequest(
            messages=messages,
            output_schema=strategy_generation.OUTPUT_SCHEMA,
            schema_name="strategy_generation",
        )

        yield _sse({"type": "step", "step": "strategy", "status": "start"})
        t0 = time.monotonic()
        try:
            output, llm_response = await _run_structured_validated(
                router, "strategy_generation", request, StrategyOutput
            )
        except ApiError as exc:
            yield _sse({"type": "error", "code": exc.code, "message": exc.message})
            await session.commit()
            return

        patch = {
            "goals": [
                {
                    "id": str(new_uuid7()),
                    "text": g.text,
                    "progress": g.progress,
                }
                for g in output.goals
            ],
            "cadence": {k: float(v) for k, v in output.cadence.items()},
        }
        yield _sse({"type": "proposal", "patch": patch})
        yield _sse({"type": "step", "step": "strategy", "status": "done"})

        usage = llm_response.usage
        task_cfg = settings.llm.tasks["strategy_generation"]
        decision_id = await _persist_decision(
            session,
            organization_id=organization_id,
            brand_id=body.brand_id,
            brand_version=brand.version,
            actor_user_id=user_id,
            kind="strategy",
            provider=task_cfg.provider,
            model=task_cfg.model,
            prompt_version=strategy_generation.PROMPT_VERSION,
            inputs_hash=_inputs_hash(body.model_dump(mode="json", by_alias=True)),
            input_summary={"brandId": str(body.brand_id)},
            output={"proposal": patch},
            status="succeeded",
            target_type="brand",
            target_id=body.brand_id,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
            latency_ms=int((time.monotonic() - t0) * 1000),
            provider_request_id=usage.provider_request_id,
        )
        await audit.record(
            session,
            organization_id=organization_id,
            brand_id=body.brand_id,
            actor_kind="user",
            actor_ref=str(user_id),
            actor_name=user_name,
            actor_user_id=user_id,
            action="ai.strategy_generated",
            target_type="ai_decision",
            target_id=decision_id,
        )
        await session.commit()
        yield _sse({"type": "done", "decisionId": str(decision_id)})

    except ApiError as exc:
        yield _sse({"type": "error", "code": exc.code, "message": exc.message})
    except asyncio.CancelledError:
        return
    except (
        ProviderContentFilteredError,
        ProviderPaymentRequiredError,
        ProviderTimeoutError,
        ProviderRateLimitedError,
        ProviderUnavailableError,
    ) as exc:
        code, message = _provider_error_code(exc)
        yield _sse({"type": "error", "code": code, "message": message})
    except Exception:
        yield _sse(
            {
                "type": "error",
                "code": "INTERNAL",
                "message": "An unexpected error occurred",
            }
        )


async def generate_alt_text(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    user_name: str,
    body: AltTextBody,
    router: LLMTaskRouter,
    settings: Settings,
    storage: ObjectStorage,
) -> AltTextResponse:
    await _rate_limit(
        organization_id, "alt_text", settings.llm.rate_limit_alt_text, settings
    )
    brand = await _resolve_brand(
        session, organization_id=organization_id, brand_id=body.brand_id
    )

    r2_key = _r2_key_from_url(body.media_url, storage)
    media_id: uuid.UUID | None = None
    if r2_key is not None:
        media_id = await repository.find_media_id_by_r2_key(
            session,
            organization_id=organization_id,
            brand_id=body.brand_id,
            r2_key=r2_key,
        )

    messages = alt_text_prompt.build(caption=body.caption)
    request = StructuredGenerationRequest(
        messages=messages,
        output_schema=alt_text_prompt.OUTPUT_SCHEMA,
        schema_name="alt_text",
    )

    t0 = time.monotonic()
    try:
        output, llm_response = await _run_structured_validated(
            router, "alt_text", request, AltTextLLMOutput
        )
    except ApiError:
        raise
    except (
        ProviderContentFilteredError,
        ProviderPaymentRequiredError,
        ProviderTimeoutError,
        ProviderRateLimitedError,
        ProviderUnavailableError,
    ) as exc:
        code, message = _provider_error_code(exc)
        raise ApiError(
            code, message, status_code=502 if code == "PROVIDER_UNAVAILABLE" else 422
        ) from exc

    task_cfg = settings.llm.tasks["alt_text"]
    decision_id = await _persist_decision(
        session,
        organization_id=organization_id,
        brand_id=body.brand_id,
        brand_version=brand.version,
        actor_user_id=user_id,
        kind="alt_text",
        provider=task_cfg.provider,
        model=task_cfg.model,
        prompt_version=alt_text_prompt.PROMPT_VERSION,
        inputs_hash=_inputs_hash(body.model_dump(mode="json", by_alias=True)),
        input_summary={"mediaUrlLength": len(body.media_url)},
        output={"alt_ar": output.alt_ar, "alt_en": output.alt_en},
        status="succeeded",
        target_type="media_asset" if media_id is not None else "brand",
        target_id=media_id or body.brand_id,
        prompt_tokens=llm_response.usage.prompt_tokens,
        completion_tokens=llm_response.usage.completion_tokens,
        cost_usd=llm_response.usage.cost_usd,
        latency_ms=int((time.monotonic() - t0) * 1000),
        provider_request_id=llm_response.usage.provider_request_id,
    )

    if media_id is not None and r2_key is not None:
        await repository.update_media_alt(
            session,
            organization_id=organization_id,
            brand_id=body.brand_id,
            r2_key=r2_key,
            alt_ar=output.alt_ar,
            alt_en=output.alt_en,
            ai_decision_id=decision_id,
        )

    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=body.brand_id,
        actor_kind="user",
        actor_ref=str(user_id),
        actor_name=user_name,
        actor_user_id=user_id,
        action="ai.alt_text_generated",
        target_type="ai_decision",
        target_id=decision_id,
    )
    await session.commit()
    return AltTextResponse(alt_ar=output.alt_ar, alt_en=output.alt_en)


async def record_feedback(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    body: AiFeedbackBody,
) -> None:
    from app.core.config import get_settings

    settings = get_settings()
    await _rate_limit(
        organization_id, "feedback", settings.llm.rate_limit_feedback, settings
    )

    try:
        decision_uuid = uuid.UUID(body.decision_id)
    except ValueError as exc:
        raise ApiError("VALIDATION", "Invalid decision id", status_code=422) from exc

    decision = await repository.get_decision(
        session,
        organization_id=organization_id,
        decision_id=decision_uuid,
    )
    if decision is None:
        raise ApiError("NOT_FOUND", "Decision not found", status_code=404)

    await repository.upsert_feedback(
        session,
        organization_id=organization_id,
        decision_id=decision_uuid,
        user_id=user_id,
        variant_index=body.variant_index,
        rating=body.rating,
    )
    await session.commit()
