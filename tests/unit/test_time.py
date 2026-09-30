from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.time import riyadh_date_key, riyadh_datetime_to_utc


def test_riyadh_datetime_to_utc_subtracts_three_hours() -> None:
    # 2026-09-04 10:00 Riyadh-local (fixed UTC+3) == 2026-09-04 07:00 UTC.
    assert riyadh_datetime_to_utc("2026-09-04", "10:00") == datetime(2026, 9, 4, 7, 0, tzinfo=UTC)


def test_riyadh_date_key_can_roll_to_the_next_day() -> None:
    # 22:30 UTC on the 3rd is already 01:30 on the 4th in Riyadh.
    instant = datetime(2026, 9, 3, 22, 30, tzinfo=UTC)
    assert riyadh_date_key(instant) == "2026-09-04"


def test_riyadh_date_key_requires_an_aware_datetime() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        riyadh_date_key(datetime(2026, 9, 4))
