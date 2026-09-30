"""CronEntry anchor floors to Riyadh Monday 06:00."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.jobs.scheduler import CronEntry

_WEEK = 604800
# Monday 06:00 Asia/Riyadh == Monday 03:00 UTC
_ANCHOR = datetime(2020, 1, 6, 3, 0, tzinfo=UTC)


def test_anchor_floors_to_monday_riyadh_week() -> None:
    entry = CronEntry(
        "analytics_weekly_insights",
        job_type="analytics_weekly_insights",
        queue="ai",
        period_seconds=_WEEK,
        anchor=_ANCHOR,
    )
    # Tuesday 2020-01-07 10:00 UTC → still in the week that started Mon 03:00 UTC
    now = datetime(2020, 1, 7, 10, 0, tzinfo=UTC)
    assert entry.current_period_start(now) == _ANCHOR

    # Next Monday 03:00 UTC exactly → new period
    next_monday = datetime(2020, 1, 13, 3, 0, tzinfo=UTC)
    assert entry.current_period_start(next_monday) == next_monday

    # Sunday before next Monday still floors to prior Monday
    sunday = datetime(2020, 1, 12, 12, 0, tzinfo=UTC)
    assert entry.current_period_start(sunday) == _ANCHOR


def test_without_anchor_uses_2020_epoch() -> None:
    entry = CronEntry(
        "analytics_sync",
        job_type="analytics_sync",
        queue="sync",
        period_seconds=900,
    )
    epoch = datetime(2020, 1, 1, tzinfo=UTC)
    now = datetime(2020, 1, 1, 0, 20, tzinfo=UTC)
    # 20 minutes past epoch floors to the 15-minute (900s) boundary.
    assert entry.current_period_start(now) == epoch + timedelta(seconds=900)
