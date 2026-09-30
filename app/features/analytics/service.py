"""Analytics sync (fleet delta) and weekly insight generation."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any

import structlog
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.time import RIYADH_TZ, riyadh_today, utc_now
from app.features import brands, notifications
from app.features.analytics import repository
from app.features.analytics.models import AnalyticsSyncState
from app.features.analytics.prompts import insight_report as insight_prompt
from app.features.analytics.schemas import InsightReportOut, InsightReportPage
from app.features.content_ai import insert_decision
from app.features.posts import Post
from app.integrations.errors import (
    AnalyticsCursorExpired,
    ProviderPaymentRequiredError,
    ProviderUnavailableError,
)
from app.integrations.llm import get_llm_router
from app.integrations.llm.ports import LLMMessage, StructuredGenerationRequest
from app.integrations.llm.router import LLMTaskRouter
from app.integrations.social import get_social_provider_for_credential
from app.integrations.social.ports import AnalyticsDeltaEntry, SocialProvider
from app.jobs.errors import RetryableError

log = structlog.get_logger(__name__)

_MAX_DELTA_PAGES = 20


class InsightNarrative(BaseModel):
    model_config = ConfigDict(extra="forbid")

    what_happened: str
    why: str
    what_to_change: str
    next_actions: list[str]


# ---------------------------------------------------------------------------
# analytics_sync
# ---------------------------------------------------------------------------


async def run_analytics_sync(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    credential_id: uuid.UUID | None = None,
) -> None:
    """Loop active credentials like zernio_health_poll and sync each fleet.

    When ``credential_id`` is set (e.g. analytics.synced webhook), sync only
    that credential.
    """
    cfg = settings or get_settings()
    credentials = await repository.list_active_credentials(session)
    for credential in credentials:
        if credential_id is not None and credential.id != credential_id:
            continue
        if credential.status == "disabled":
            continue
        provider = get_social_provider_for_credential(
            cfg, alias=credential.alias, secret_ref=credential.secret_ref
        )
        try:
            await sync_credential_analytics(
                session, credential_id=credential.id, provider=provider
            )
        except RetryableError:
            raise
        except Exception:
            log.exception(
                "analytics_sync.credential_failed",
                credential_id=str(credential.id),
                alias=credential.alias,
            )


async def sync_credential_analytics(
    session: AsyncSession,
    *,
    credential_id: uuid.UUID,
    provider: SocialProvider,
) -> None:
    state = await repository.get_or_create_sync_state(
        session, credential_id=credential_id
    )
    if state.bootstrapped_at is None:
        await _bootstrap_credential(session, state=state, provider=provider)
        return
    await _delta_sync(session, state=state, provider=provider)


async def _bootstrap_credential(
    session: AsyncSession,
    *,
    state: AnalyticsSyncState,
    provider: SocialProvider,
) -> None:
    try:
        baseline = await provider.get_analytics_baseline()
    except ProviderPaymentRequiredError:
        await _gate(session, state=state)
        return
    except ProviderUnavailableError as exc:
        raise RetryableError(after=exc.retry_after) from exc

    if not baseline.has_analytics_access:
        await _gate(session, state=state)
        return

    today = riyadh_today()
    for account in baseline.accounts:
        social = await repository.get_account_by_zernio_id(
            session, zernio_account_id=account.account_id
        )
        if social is None:
            continue
        await repository.upsert_account_metric_snapshot(
            session,
            organization_id=social.organization_id,
            brand_id=social.brand_id,
            social_account_id=social.id,
            platform=social.platform or account.platform,
            metric_date=today,
            followers=account.follower_count,
            raw={"source": "baseline", "account_id": account.account_id},
        )

    try:
        delta = await provider.get_analytics_delta(cursor=None)
    except ProviderPaymentRequiredError:
        await _gate(session, state=state)
        return
    except ProviderUnavailableError as exc:
        raise RetryableError(after=exc.retry_after) from exc
    except AnalyticsCursorExpired:
        # No cursor on bootstrap — treat as unavailable retry.
        raise RetryableError(after=30.0) from None

    # Bootstrap always stores next_cursor even when the first page is empty.
    if delta.data:
        skipped = await _apply_delta_entries(session, entries=delta.data)
        log.info(
            "analytics_sync.bootstrap_page",
            credential_id=str(state.credential_id),
            entries=len(delta.data),
            skipped=skipped,
        )

    await repository.mark_sync_ok(
        session,
        state=state,
        last_cursor=delta.next_cursor,
        bootstrapped_at=utc_now(),
    )


async def _delta_sync(
    session: AsyncSession,
    *,
    state: AnalyticsSyncState,
    provider: SocialProvider,
) -> None:
    cursor: str | None = state.last_cursor
    pages = 0
    while pages < _MAX_DELTA_PAGES:
        pages += 1
        try:
            delta = await provider.get_analytics_delta(cursor=cursor)
        except ProviderPaymentRequiredError:
            await _gate(session, state=state)
            return
        except AnalyticsCursorExpired:
            await repository.clear_bootstrap(session, state=state)
            await _bootstrap_credential(session, state=state, provider=provider)
            return
        except ProviderUnavailableError as exc:
            raise RetryableError(after=exc.retry_after) from exc

        if not delta.data:
            # Empty page: do NOT advance last_cursor.
            await repository.mark_sync_ok(session, state=state)
            return

        skipped = await _apply_delta_entries(session, entries=delta.data)
        log.info(
            "analytics_sync.page",
            credential_id=str(state.credential_id),
            entries=len(delta.data),
            skipped=skipped,
            has_more=delta.has_more,
        )
        cursor = delta.next_cursor
        await repository.mark_sync_ok(session, state=state, last_cursor=cursor)
        if not delta.has_more:
            return


async def _apply_delta_entries(
    session: AsyncSession, *, entries: list[AnalyticsDeltaEntry]
) -> int:
    skipped = 0
    for entry in entries:
        if entry.is_deleted:
            skipped += 1
            continue
        publication = await repository.get_publication_by_zernio_post_id(
            session, zernio_post_id=entry.post_id
        )
        if publication is None:
            skipped += 1
            continue
        metric_date = _metric_date_for_entry(entry)
        await repository.upsert_post_metric_snapshot(
            session,
            organization_id=publication.organization_id,
            brand_id=publication.brand_id,
            social_account_id=publication.social_account_id,
            platform=entry.platform or "instagram",
            post_id=publication.post_id,
            metric_date=metric_date,
            metrics=dict(entry.metrics),
            raw=entry.model_dump(mode="json"),
        )
    return skipped


async def _gate(session: AsyncSession, *, state: AnalyticsSyncState) -> None:
    await repository.mark_sync_gated(
        session, state=state, error="analytics_addon_required"
    )
    log.info(
        "analytics_sync_gated",
        credential_id=str(state.credential_id),
        error="analytics_addon_required",
    )


def _metric_date_for_entry(entry: AnalyticsDeltaEntry) -> date:
    for raw in (entry.published_at, entry.synced_at):
        if not raw:
            continue
        try:
            instant = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=RIYADH_TZ)
        return instant.astimezone(RIYADH_TZ).date()
    return riyadh_today()


# ---------------------------------------------------------------------------
# analytics_weekly_insights
# ---------------------------------------------------------------------------


def previous_riyadh_week_monday(today: date | None = None) -> date:
    """Monday of the previous completed Riyadh week (Mon–Sun)."""
    d = today or riyadh_today()
    this_monday = d - timedelta(days=d.weekday())
    return this_monday - timedelta(days=7)


async def run_weekly_insights(
    session: AsyncSession,
    *,
    settings: Settings | None = None,
    router: LLMTaskRouter | None = None,
    week_of: date | None = None,
) -> int:
    """Generate insight reports for eligible brands. Returns insert count."""
    cfg = settings or get_settings()
    llm = router or get_llm_router(cfg)
    target_week = week_of or previous_riyadh_week_monday()
    week_end = target_week + timedelta(days=6)
    prior_week = target_week - timedelta(days=7)
    prior_end = target_week - timedelta(days=1)

    brands_rows = await repository.list_brands_eligible_for_insights(session)
    inserted = 0
    for organization_id, brand_id in brands_rows:
        existing = await repository.get_insight_report(
            session, brand_id=brand_id, week_of=target_week
        )
        if existing is not None:
            continue

        snapshots = await repository.list_metric_snapshots_for_week(
            session, brand_id=brand_id, week_start=target_week, week_end=week_end
        )
        if not snapshots:
            continue

        prior = await repository.list_metric_snapshots_for_week(
            session, brand_id=brand_id, week_start=prior_week, week_end=prior_end
        )
        brand = await brands.get_brand(
            session, organization_id=organization_id, brand_id=brand_id
        )
        if brand is None:
            continue

        pillar_names = {p.id: p.name for p in brand.pillars}
        post_pillars = await _post_pillar_map(
            session, post_ids=[s.post_id for s in snapshots if s.post_id is not None]
        )
        agg = _aggregate_week(
            snapshots, prior, post_pillars=post_pillars, pillar_names=pillar_names
        )
        languages = (brand.guidelines.languages if brand.guidelines else None) or ["ar"]
        language = languages[0] if languages else "ar"

        brand_ctx = {
            "name": brand.name,
            "industry": brand.industry,
            "city": brand.city,
            "guidelines": brand.guidelines.model_dump(mode="json", by_alias=True)
            if brand.guidelines
            else {},
        }
        messages = insight_prompt.build(
            brand=brand_ctx,
            week_of=target_week.isoformat(),
            metrics=agg["metrics"],
            series=agg["series"],
            pillar_breakdown=agg["pillarBreakdown"],
            platform_breakdown=agg["platformBreakdown"],
            language=language,
        )
        request = StructuredGenerationRequest(
            messages=messages,
            output_schema=insight_prompt.OUTPUT_SCHEMA,
            schema_name="insight_report",
        )
        t0 = time.monotonic()
        try:
            narrative, llm_response = await _run_structured_validated(
                llm, "insight_report", request, InsightNarrative
            )
        except ValidationError:
            log.warning(
                "analytics_weekly_insights.invalid_output",
                brand_id=str(brand_id),
                week_of=target_week.isoformat(),
            )
            continue

        task_cfg = cfg.llm.tasks["insight_report"]
        inputs = {
            "brandId": str(brand_id),
            "weekOf": target_week.isoformat(),
            "metrics": agg["metrics"],
        }
        decision = await insert_decision(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            brand_version=brand.version,
            actor_user_id=None,
            kind="insight",
            provider=task_cfg.provider,
            model=task_cfg.model,
            prompt_version=insight_prompt.PROMPT_VERSION,
            inputs_hash=_inputs_hash(inputs),
            input_summary={"weekOf": target_week.isoformat()},
            output=narrative.model_dump(),
            status="succeeded",
            target_type="brand",
            target_id=brand_id,
            prompt_tokens=llm_response.usage.prompt_tokens,
            completion_tokens=llm_response.usage.completion_tokens,
            cost_usd=llm_response.usage.cost_usd,
            latency_ms=int((time.monotonic() - t0) * 1000),
            provider_request_id=llm_response.usage.provider_request_id,
        )

        report = await repository.insert_insight_report_ignore_conflict(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            week_of=target_week,
            what_happened=narrative.what_happened,
            why=narrative.why,
            what_to_change=narrative.what_to_change,
            next_actions=list(narrative.next_actions),
            top_post_id=agg["top_post_id"],
            metrics=agg["metrics"],
            series=agg["series"],
            pillar_breakdown=agg["pillarBreakdown"],
            platform_breakdown=agg["platformBreakdown"],
            decision_id=decision.id,
            generated_at=utc_now(),
        )
        if report is None:
            continue

        inserted += 1
        await notifications.fan_out(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            type="weekly_insight",
            message_key="weekly_insight",
            params={"weekOf": target_week.isoformat()},
            target_href="/analytics",
            actor_user_id=None,
            actor_ref="system:analytics_weekly_insights",
        )
    return inserted


async def _post_pillar_map(
    session: AsyncSession, *, post_ids: list[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID | None]:
    unique = list({pid for pid in post_ids})
    if not unique:
        return {}
    from sqlalchemy import select

    rows = (
        await session.execute(select(Post.id, Post.pillar_id).where(Post.id.in_(unique)))
    ).all()
    return {row.id: row.pillar_id for row in rows}


def _aggregate_week(
    snapshots: list[Any],
    prior: list[Any],
    *,
    post_pillars: dict[uuid.UUID, uuid.UUID | None],
    pillar_names: dict[uuid.UUID, str],
) -> dict[str, Any]:
    post_snaps = [s for s in snapshots if s.post_id is not None]
    account_snaps = [s for s in snapshots if s.post_id is None]
    prior_post = [s for s in prior if s.post_id is not None]
    prior_account = [s for s in prior if s.post_id is None]

    reach = sum(_reach(s) for s in post_snaps)
    engagement = sum(_engagement(s) for s in post_snaps)
    prior_reach = sum(_reach(s) for s in prior_post)
    prior_engagement = sum(_engagement(s) for s in prior_post)

    followers = _latest_followers(account_snaps)
    prior_followers = _latest_followers(prior_account)

    metrics = {
        "reach": reach,
        "reachDelta": _pct_delta(reach, prior_reach),
        "engagement": engagement,
        "engagementDelta": _pct_delta(engagement, prior_engagement),
        "followers": followers,
        "followersDelta": _pct_delta(followers, prior_followers),
    }

    by_date: dict[date, dict[str, int]] = {}
    for s in post_snaps:
        bucket = by_date.setdefault(s.metric_date, {"reach": 0, "engagement": 0})
        bucket["reach"] += _reach(s)
        bucket["engagement"] += _engagement(s)
    series = [
        {
            "date": d.isoformat(),
            "reach": vals["reach"],
            "engagement": vals["engagement"],
        }
        for d, vals in sorted(by_date.items())
    ]

    pillar_map: dict[uuid.UUID, dict[str, Any]] = {}
    platform_map: dict[str, dict[str, Any]] = {}
    post_reach: dict[uuid.UUID, int] = {}
    for s in post_snaps:
        assert s.post_id is not None
        post_reach[s.post_id] = post_reach.get(s.post_id, 0) + _reach(s)
        plat = s.platform or "instagram"
        prow = platform_map.setdefault(
            plat, {"platform": plat, "reach": 0, "engagement": 0, "posts": set()}
        )
        prow["reach"] += _reach(s)
        prow["engagement"] += _engagement(s)
        prow["posts"].add(s.post_id)

        pillar_id = post_pillars.get(s.post_id)
        if pillar_id is not None:
            brow = pillar_map.setdefault(
                pillar_id,
                {
                    "pillarId": str(pillar_id),
                    "pillarName": pillar_names.get(pillar_id, "Unknown"),
                    "reach": 0,
                    "engagement": 0,
                },
            )
            brow["reach"] += _reach(s)
            brow["engagement"] += _engagement(s)

    pillar_breakdown: list[dict[str, Any]] = list(pillar_map.values())

    platform_breakdown = [
        {
            "platform": v["platform"],
            "reach": v["reach"],
            "engagement": v["engagement"],
            "posts": len(v["posts"]),
        }
        for v in sorted(platform_map.values(), key=lambda x: str(x["platform"]))
    ]

    top_post_id: uuid.UUID | None = None
    if post_reach:
        top_post_id = max(post_reach.items(), key=lambda kv: kv[1])[0]

    return {
        "metrics": metrics,
        "series": series,
        "pillarBreakdown": pillar_breakdown,
        "platformBreakdown": platform_breakdown,
        "top_post_id": top_post_id,
    }


def _reach(snapshot: Any) -> int:
    if snapshot.reach is not None:
        return int(snapshot.reach)
    if snapshot.impressions is not None:
        return int(snapshot.impressions)
    return 0


def _engagement(snapshot: Any) -> int:
    total = 0
    for field in ("likes", "comments", "shares", "saves"):
        value = getattr(snapshot, field, None)
        if value is not None:
            total += int(value)
    return total


def _latest_followers(account_snaps: list[Any]) -> int:
    if not account_snaps:
        return 0
    newest = max(account_snaps, key=lambda s: (s.metric_date, s.captured_at))
    return int(newest.followers or 0)


def _pct_delta(current: float, prior: float) -> float:
    if prior == 0:
        return 0.0 if current == 0 else 100.0
    return round(((current - prior) / prior) * 100.0, 1)


def _inputs_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


async def _run_structured_validated[T: BaseModel](
    router: LLMTaskRouter,
    task: str,
    request: StructuredGenerationRequest,
    model: type[T],
) -> tuple[T, Any]:
    last_error: ValidationError | None = None
    messages = list(request.messages)
    for attempt in range(2):
        req = request.model_copy(update={"messages": messages})
        response = await router.run_structured(task, req)
        try:
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
    raise last_error


# ---------------------------------------------------------------------------
# HTTP read surface (Stage 7)
# ---------------------------------------------------------------------------


def report_to_out(report: Any) -> InsightReportOut:
    from app.features.analytics.schemas import (
        metrics_from_raw,
        pillar_breakdown_from_raw,
        platform_breakdown_from_raw,
        series_from_raw,
    )

    return InsightReportOut(
        id=report.id,
        organization_id=report.organization_id,
        brand_id=report.brand_id,
        week_of=report.week_of,
        what_happened=report.what_happened,
        why=report.why,
        what_to_change=report.what_to_change,
        next_actions=list(report.next_actions or []),
        top_post_id=report.top_post_id,
        metrics=metrics_from_raw(dict(report.metrics or {})),
        series=series_from_raw(report.series),
        pillar_breakdown=pillar_breakdown_from_raw(report.pillar_breakdown),
        platform_breakdown=platform_breakdown_from_raw(report.platform_breakdown),
    )


async def list_insight_reports(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    cursor: str | None = None,
    limit: int = 20,
) -> InsightReportPage:
    from app.core.errors import ApiError
    from app.core.pagination import InvalidCursorError

    brand = await brands.get_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )
    if brand is None:
        raise ApiError("NOT_FOUND", "Brand not found", status_code=404)
    try:
        rows, next_cursor = await repository.list_insight_reports_page(
            session,
            organization_id=organization_id,
            brand_id=brand_id,
            cursor=cursor,
            limit=limit,
        )
    except (InvalidCursorError, KeyError, ValueError, TypeError) as exc:
        raise ApiError("VALIDATION", "Invalid pagination cursor", status_code=422) from exc
    return InsightReportPage(
        items=[report_to_out(row) for row in rows], next_cursor=next_cursor
    )


async def get_latest_insight_report(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
) -> InsightReportOut:
    from app.core.errors import ApiError

    brand = await brands.get_brand(
        session, organization_id=organization_id, brand_id=brand_id
    )
    if brand is None:
        raise ApiError("NOT_FOUND", "Brand not found", status_code=404)
    report = await repository.get_latest_insight_report(
        session, organization_id=organization_id, brand_id=brand_id
    )
    if report is None:
        raise ApiError("NOT_FOUND", "No insight report yet", status_code=404)
    return report_to_out(report)
