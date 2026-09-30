"""Analytics persistence — sync state, metric snapshots, insight reports."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.features.analytics.models import (
    AnalyticsSyncState,
    InsightReport,
    MetricSnapshot,
)
from app.features.publishing import Publication
from app.features.social_accounts import SocialAccount, ZernioCredential


async def list_active_credentials(session: AsyncSession) -> list[ZernioCredential]:
    stmt = sa.select(ZernioCredential).where(ZernioCredential.status == "active")
    return list((await session.execute(stmt)).scalars().all())


async def get_or_create_sync_state(
    session: AsyncSession, *, credential_id: uuid.UUID
) -> AnalyticsSyncState:
    row = await session.get(AnalyticsSyncState, credential_id)
    if row is not None:
        return row
    stmt = (
        insert(AnalyticsSyncState)
        .values(credential_id=credential_id)
        .on_conflict_do_nothing(index_elements=["credential_id"])
        .returning(AnalyticsSyncState.credential_id)
    )
    await session.execute(stmt)
    await session.flush()
    row = await session.get(AnalyticsSyncState, credential_id)
    if row is None:
        raise RuntimeError(f"analytics_sync_state missing for {credential_id}")
    return row


async def get_account_by_zernio_id(
    session: AsyncSession, *, zernio_account_id: str
) -> SocialAccount | None:
    stmt = sa.select(SocialAccount).where(
        SocialAccount.zernio_account_id == zernio_account_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_publication_by_zernio_post_id(
    session: AsyncSession, *, zernio_post_id: str
) -> Publication | None:
    stmt = sa.select(Publication).where(Publication.zernio_post_id == zernio_post_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def upsert_post_metric_snapshot(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    social_account_id: uuid.UUID,
    platform: str,
    post_id: uuid.UUID,
    metric_date: date,
    metrics: dict[str, Any],
    raw: dict[str, Any] | None,
) -> None:
    values = {
        "organization_id": organization_id,
        "brand_id": brand_id,
        "social_account_id": social_account_id,
        "platform": platform,
        "post_id": post_id,
        "metric_date": metric_date,
        "impressions": _int_or_none(metrics.get("impressions")),
        "reach": _int_or_none(metrics.get("reach")),
        "likes": _int_or_none(metrics.get("likes")),
        "comments": _int_or_none(metrics.get("comments")),
        "shares": _int_or_none(metrics.get("shares")),
        "saves": _int_or_none(metrics.get("saves")),
        "video_views": _int_or_none(metrics.get("video_views") or metrics.get("videoViews")),
        "clicks": _int_or_none(metrics.get("clicks")),
        "followers": _int_or_none(metrics.get("followers") or metrics.get("follower_count")),
        "raw": raw,
        "captured_at": utc_now(),
    }
    stmt = insert(MetricSnapshot).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["post_id", "metric_date"],
        index_where=sa.text("post_id IS NOT NULL"),
        set_={
            "impressions": stmt.excluded.impressions,
            "reach": stmt.excluded.reach,
            "likes": stmt.excluded.likes,
            "comments": stmt.excluded.comments,
            "shares": stmt.excluded.shares,
            "saves": stmt.excluded.saves,
            "video_views": stmt.excluded.video_views,
            "clicks": stmt.excluded.clicks,
            "followers": stmt.excluded.followers,
            "raw": stmt.excluded.raw,
            "captured_at": stmt.excluded.captured_at,
            "platform": stmt.excluded.platform,
            "social_account_id": stmt.excluded.social_account_id,
        },
    )
    await session.execute(stmt)


async def upsert_account_metric_snapshot(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    social_account_id: uuid.UUID,
    platform: str,
    metric_date: date,
    followers: int | None,
    raw: dict[str, Any] | None = None,
) -> None:
    values = {
        "organization_id": organization_id,
        "brand_id": brand_id,
        "social_account_id": social_account_id,
        "platform": platform,
        "post_id": None,
        "metric_date": metric_date,
        "followers": followers,
        "raw": raw,
        "captured_at": utc_now(),
    }
    stmt = insert(MetricSnapshot).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["social_account_id", "metric_date"],
        index_where=sa.text("post_id IS NULL"),
        set_={
            "followers": stmt.excluded.followers,
            "raw": stmt.excluded.raw,
            "captured_at": stmt.excluded.captured_at,
            "platform": stmt.excluded.platform,
        },
    )
    await session.execute(stmt)


async def mark_sync_ok(
    session: AsyncSession,
    *,
    state: AnalyticsSyncState,
    last_cursor: str | None = None,
    bootstrapped_at: datetime | None = None,
) -> None:
    if last_cursor is not None:
        state.last_cursor = last_cursor
    if bootstrapped_at is not None:
        state.bootstrapped_at = bootstrapped_at
    state.last_synced_at = utc_now()
    state.last_sync_status = "ok"
    state.last_error = None
    state.consecutive_failures = 0
    state.updated_at = utc_now()
    await session.flush()


async def mark_sync_gated(
    session: AsyncSession, *, state: AnalyticsSyncState, error: str
) -> None:
    state.last_sync_status = "gated"
    state.last_error = error
    state.last_synced_at = utc_now()
    state.updated_at = utc_now()
    await session.flush()


async def mark_sync_error(
    session: AsyncSession, *, state: AnalyticsSyncState, error: str
) -> None:
    state.last_sync_status = "error"
    state.last_error = error
    state.consecutive_failures = int(state.consecutive_failures or 0) + 1
    state.updated_at = utc_now()
    await session.flush()


async def clear_bootstrap(session: AsyncSession, *, state: AnalyticsSyncState) -> None:
    state.bootstrapped_at = None
    state.last_cursor = None
    state.updated_at = utc_now()
    await session.flush()


async def list_brands_eligible_for_insights(
    session: AsyncSession,
) -> Sequence[tuple[uuid.UUID, uuid.UUID]]:
    """Brands with ≥1 published post and ≥1 connected account."""
    from app.features.posts import Post

    published = (
        sa.select(Post.brand_id)
        .where(Post.status == "published", Post.deleted_at.is_(None))
        .distinct()
        .subquery()
    )
    connected = (
        sa.select(SocialAccount.brand_id)
        .where(
            SocialAccount.status.in_(("connected", "expiring")),
            SocialAccount.zernio_account_id.is_not(None),
        )
        .distinct()
        .subquery()
    )
    stmt = (
        sa.select(Post.organization_id, Post.brand_id)
        .where(Post.brand_id.in_(sa.select(published.c.brand_id)))
        .where(Post.brand_id.in_(sa.select(connected.c.brand_id)))
        .where(Post.status == "published", Post.deleted_at.is_(None))
        .distinct()
    )
    rows = (await session.execute(stmt)).all()
    return [(row.organization_id, row.brand_id) for row in rows]


async def list_metric_snapshots_for_week(
    session: AsyncSession,
    *,
    brand_id: uuid.UUID,
    week_start: date,
    week_end: date,
) -> list[MetricSnapshot]:
    stmt = sa.select(MetricSnapshot).where(
        MetricSnapshot.brand_id == brand_id,
        MetricSnapshot.metric_date >= week_start,
        MetricSnapshot.metric_date <= week_end,
    )
    return list((await session.execute(stmt)).scalars().all())


async def insert_insight_report_ignore_conflict(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    week_of: date,
    what_happened: str,
    why: str,
    what_to_change: str,
    next_actions: list[str],
    top_post_id: uuid.UUID | None,
    metrics: dict[str, Any],
    series: list[dict[str, Any]] | dict[str, Any],
    pillar_breakdown: list[dict[str, Any]] | dict[str, Any],
    platform_breakdown: list[dict[str, Any]] | dict[str, Any],
    decision_id: uuid.UUID | None,
    generated_at: datetime,
) -> InsightReport | None:
    """Insert insight report; ON CONFLICT (brand_id, week_of) DO NOTHING."""
    stmt = (
        insert(InsightReport)
        .values(
            organization_id=organization_id,
            brand_id=brand_id,
            week_of=week_of,
            what_happened=what_happened,
            why=why,
            what_to_change=what_to_change,
            next_actions=next_actions,
            top_post_id=top_post_id,
            metrics=metrics,
            series=series,
            pillar_breakdown=pillar_breakdown,
            platform_breakdown=platform_breakdown,
            decision_id=decision_id,
            generated_at=generated_at,
        )
        .on_conflict_do_nothing(constraint="uq_insight_reports_brand_id_week_of")
        .returning(InsightReport)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def get_insight_report(
    session: AsyncSession, *, brand_id: uuid.UUID, week_of: date
) -> InsightReport | None:
    stmt = sa.select(InsightReport).where(
        InsightReport.brand_id == brand_id,
        InsightReport.week_of == week_of,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


DEFAULT_INSIGHT_PAGE_SIZE = 20
MAX_INSIGHT_PAGE_SIZE = 100


async def list_insight_reports_page(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    cursor: str | None,
    limit: int = DEFAULT_INSIGHT_PAGE_SIZE,
) -> tuple[list[InsightReport], str | None]:
    from app.core.pagination import decode_cursor, encode_cursor

    limit = min(max(limit, 1), MAX_INSIGHT_PAGE_SIZE)
    stmt = sa.select(InsightReport).where(
        InsightReport.organization_id == organization_id,
        InsightReport.brand_id == brand_id,
    )
    if cursor is not None:
        parsed = decode_cursor(cursor)
        cursor_week = date.fromisoformat(str(parsed["week_of"])[:10])
        cursor_id = uuid.UUID(str(parsed["id"]))
        stmt = stmt.where(
            sa.tuple_(InsightReport.week_of, InsightReport.id)
            < (cursor_week, cursor_id)
        )
    stmt = stmt.order_by(InsightReport.week_of.desc(), InsightReport.id.desc()).limit(
        limit + 1
    )
    rows = list((await session.execute(stmt)).scalars().all())
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = encode_cursor(
            {"week_of": last.week_of.isoformat(), "id": str(last.id)}
        )
    return page, next_cursor


async def get_latest_insight_report(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
) -> InsightReport | None:
    stmt = (
        sa.select(InsightReport)
        .where(
            InsightReport.organization_id == organization_id,
            InsightReport.brand_id == brand_id,
        )
        .order_by(InsightReport.week_of.desc(), InsightReport.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None
