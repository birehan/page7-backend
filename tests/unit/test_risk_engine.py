"""Risk-engine parity against fixtures generated from the frontend computeRisk."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from app.features.posts.risk_engine import (
    compute_risk,
    find_overlapping_prayer,
    prayer_times_for,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "risk_parity.json"


def _load_fixtures() -> list[dict[str, Any]]:
    if not FIXTURE.exists():
        pytest.skip(
            "risk_parity.json not generated — run pgblank-web/scripts/gen-risk-fixtures.ts"
        )
    data: dict[str, Any] = json.loads(FIXTURE.read_text())
    fixtures: list[dict[str, Any]] = data["fixtures"]
    return fixtures


@pytest.mark.parametrize("fixture", _load_fixtures(), ids=lambda f: f["id"])
def test_risk_parity_with_frontend_fixture(fixture: dict[str, Any]) -> None:
    inp = fixture["input"]
    scheduled = (
        datetime.fromisoformat(inp["scheduledAt"].replace("Z", "+00:00"))
        if inp.get("scheduledAt")
        else None
    )
    report = compute_risk(
        variants=inp["variants"],
        platforms=inp["platforms"],
        banned_claims=inp.get("bannedClaims"),
        media_alts=inp.get("mediaAlts"),
        scheduled_at=scheduled,
        city=inp.get("city"),
        is_paid=bool(inp.get("isPaid", False)),
    )
    expected = fixture["expected"]
    assert report.score == expected["score"]
    assert [r.to_dict() for r in report.reasons] == expected["reasons"]


def test_prayer_summer_season() -> None:
    times = prayer_times_for("2026-07-15")
    assert ("dhuhr", "12:15") in times


def test_prayer_winter_season() -> None:
    times = prayer_times_for("2026-01-15")
    assert ("fajr", "05:10") in times


def test_prayer_jeddah_offset() -> None:
    times = dict(prayer_times_for("2026-07-15", "Jeddah"))
    assert times["maghrib"] == "19:00"  # 18:45 + 15


def test_prayer_exact_boundary_plus_20() -> None:
    # Summer Dhuhr 12:15; +20min = 12:35 still overlaps
    assert find_overlapping_prayer("2026-07-15", "12:35") == "dhuhr"
    # +21min = 12:36 does not
    assert find_overlapping_prayer("2026-07-15", "12:36") is None


def test_prayer_exact_boundary_minus_20() -> None:
    assert find_overlapping_prayer("2026-07-15", "11:55") == "dhuhr"
    assert find_overlapping_prayer("2026-07-15", "11:54") is None
