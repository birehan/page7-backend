"""Wire schemas for analytics insight-report endpoints — camelCase via CamelModel."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from app.core.pagination import Page
from app.core.schema import CamelModel


class InsightMetricsOut(CamelModel):
    reach: float
    reach_delta: float
    engagement: float
    engagement_delta: float
    followers: float
    followers_delta: float


class InsightSeriesPointOut(CamelModel):
    date: date
    reach: float
    engagement: float


class InsightPillarBreakdownOut(CamelModel):
    pillar_id: str
    pillar_name: str
    reach: float
    engagement: float


class InsightPlatformBreakdownOut(CamelModel):
    platform: str
    reach: float
    engagement: float
    posts: int


class InsightReportOut(CamelModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    brand_id: uuid.UUID
    week_of: date
    what_happened: str
    why: str
    what_to_change: str
    next_actions: list[str]
    top_post_id: uuid.UUID | None = None
    metrics: InsightMetricsOut
    series: list[InsightSeriesPointOut]
    pillar_breakdown: list[InsightPillarBreakdownOut]
    platform_breakdown: list[InsightPlatformBreakdownOut]


InsightReportPage = Page[InsightReportOut]


def metrics_from_raw(raw: dict[str, Any]) -> InsightMetricsOut:
    return InsightMetricsOut(
        reach=float(raw.get("reach") or 0),
        reach_delta=float(raw.get("reachDelta") or raw.get("reach_delta") or 0),
        engagement=float(raw.get("engagement") or 0),
        engagement_delta=float(
            raw.get("engagementDelta") or raw.get("engagement_delta") or 0
        ),
        followers=float(raw.get("followers") or 0),
        followers_delta=float(
            raw.get("followersDelta") or raw.get("followers_delta") or 0
        ),
    )


def series_from_raw(raw: Any) -> list[InsightSeriesPointOut]:
    if not isinstance(raw, list):
        return []
    points: list[InsightSeriesPointOut] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        day = item.get("date")
        if day is None:
            continue
        if isinstance(day, datetime):
            day = day.date()
        elif isinstance(day, str):
            day = date.fromisoformat(day[:10])
        points.append(
            InsightSeriesPointOut(
                date=day,
                reach=float(item.get("reach") or 0),
                engagement=float(item.get("engagement") or 0),
            )
        )
    return points


def pillar_breakdown_from_raw(raw: Any) -> list[InsightPillarBreakdownOut]:
    if not isinstance(raw, list):
        return []
    rows: list[InsightPillarBreakdownOut] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rows.append(
            InsightPillarBreakdownOut(
                pillar_id=str(item.get("pillarId") or item.get("pillar_id") or ""),
                pillar_name=str(
                    item.get("pillarName") or item.get("pillar_name") or ""
                ),
                reach=float(item.get("reach") or 0),
                engagement=float(item.get("engagement") or 0),
            )
        )
    return rows


def platform_breakdown_from_raw(raw: Any) -> list[InsightPlatformBreakdownOut]:
    if not isinstance(raw, list):
        return []
    rows: list[InsightPlatformBreakdownOut] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        rows.append(
            InsightPlatformBreakdownOut(
                platform=str(item.get("platform") or "instagram"),
                reach=float(item.get("reach") or 0),
                engagement=float(item.get("engagement") or 0),
                posts=int(item.get("posts") or 0),
            )
        )
    return rows
