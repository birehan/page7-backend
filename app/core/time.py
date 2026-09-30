from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

RIYADH_TZ = ZoneInfo("Asia/Riyadh")  # fixed UTC+3, no DST


def utc_now() -> datetime:
    return datetime.now(UTC)


def riyadh_today() -> date:
    return datetime.now(RIYADH_TZ).date()


def riyadh_day_start_utc(instant: datetime | None = None) -> datetime:
    """UTC instant of 00:00 Asia/Riyadh on the Riyadh calendar day of `instant`."""
    when = instant if instant is not None else utc_now()
    if when.tzinfo is None:
        raise ValueError("instant must be timezone-aware")
    local_day = when.astimezone(RIYADH_TZ).date()
    return datetime(local_day.year, local_day.month, local_day.day, tzinfo=RIYADH_TZ).astimezone(
        UTC
    )


def seconds_until_next_riyadh_midnight(instant: datetime | None = None) -> int:
    when = instant if instant is not None else utc_now()
    start = riyadh_day_start_utc(when)
    from datetime import timedelta

    next_start = start + timedelta(days=1)
    return max(1, int((next_start - when.astimezone(UTC)).total_seconds()))


def riyadh_date_key(instant: datetime) -> str:
    """The Riyadh-local calendar day an instant falls on, as YYYY-MM-DD."""
    if instant.tzinfo is None:
        raise ValueError("instant must be timezone-aware")
    return instant.astimezone(RIYADH_TZ).date().isoformat()


def riyadh_datetime_to_utc(date_key: str, time_str: str) -> datetime:
    """Combine a Riyadh-local date key (YYYY-MM-DD) and time (HH:MM) into a UTC
    instant. This is the one place this conversion happens server-side — the
    current-state audit found two places in the mock frontend that instead parse
    the equivalent string as browser-local time, silently misscheduling posts for
    any viewer not in the Riyadh offset. That bug gets fixed in the frontend in
    Phase 6; this function is the server-side contract it must match.
    """
    naive = datetime.fromisoformat(f"{date_key}T{time_str}:00")
    return naive.replace(tzinfo=RIYADH_TZ).astimezone(UTC)
