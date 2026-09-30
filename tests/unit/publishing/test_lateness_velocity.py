"""Unit tests for lateness window and velocity next-slot math."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.time import riyadh_day_start_utc
from app.features.publishing.service import (
    check_lateness,
    compute_velocity_hold_until,
)


def test_lateness_inside_window() -> None:
    run_at = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    now = run_at + timedelta(minutes=29)
    assert check_lateness(run_at, now=now, window_seconds=1800) is False


def test_lateness_exactly_at_window_is_not_missed() -> None:
    run_at = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    now = run_at + timedelta(seconds=1800)
    assert check_lateness(run_at, now=now, window_seconds=1800) is False


def test_lateness_past_window() -> None:
    run_at = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
    now = run_at + timedelta(seconds=1801)
    assert check_lateness(run_at, now=now, window_seconds=1800) is True


def test_lateness_rejects_naive_datetimes() -> None:
    run_at = datetime(2026, 9, 5, 10, 0)
    now = datetime(2026, 9, 5, 11, 0, tzinfo=UTC)
    with pytest.raises(ValueError):
        check_lateness(run_at, now=now)


def test_velocity_ok_under_limits() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    assert (
        compute_velocity_hold_until(
            platform="instagram",
            now=now,
            hourly_count=24,
            hourly_oldest=now - timedelta(minutes=30),
            daily_count=99,
            daily_oldest=now - timedelta(hours=2),
        )
        is None
    )


def test_velocity_hourly_hold_uses_oldest_plus_one_hour() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    oldest = now - timedelta(minutes=20)
    held = compute_velocity_hold_until(
        platform="instagram",
        now=now,
        hourly_count=25,
        hourly_oldest=oldest,
        daily_count=10,
        daily_oldest=None,
    )
    assert held == oldest + timedelta(hours=1)


def test_velocity_hourly_hold_without_oldest_defaults_one_hour() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    held = compute_velocity_hold_until(
        platform="facebook",
        now=now,
        hourly_count=25,
        hourly_oldest=None,
        daily_count=0,
        daily_oldest=None,
    )
    assert held == now + timedelta(hours=1)


def test_velocity_daily_hold_instagram_next_riyadh_midnight() -> None:
    # 12:00 UTC = 15:00 Riyadh on 2026-09-05
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    held = compute_velocity_hold_until(
        platform="instagram",
        now=now,
        hourly_count=0,
        hourly_oldest=None,
        daily_count=100,
        daily_oldest=None,
    )
    assert held == riyadh_day_start_utc(now) + timedelta(days=1)


def test_velocity_daily_tiktok_cap_is_50() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    assert (
        compute_velocity_hold_until(
            platform="tiktok",
            now=now,
            hourly_count=0,
            hourly_oldest=None,
            daily_count=49,
            daily_oldest=None,
        )
        is None
    )
    held = compute_velocity_hold_until(
        platform="tiktok",
        now=now,
        hourly_count=0,
        hourly_oldest=None,
        daily_count=50,
        daily_oldest=None,
    )
    assert held is not None


def test_velocity_takes_later_of_hourly_and_daily() -> None:
    now = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    hourly_until = now + timedelta(minutes=10)
    held = compute_velocity_hold_until(
        platform="snapchat",
        now=now,
        hourly_count=25,
        hourly_oldest=hourly_until - timedelta(hours=1),
        daily_count=50,
        daily_oldest=None,
    )
    daily_until = riyadh_day_start_utc(now) + timedelta(days=1)
    assert held == max(hourly_until, daily_until)
