"""Unit checks for reconcile threshold constants."""

from __future__ import annotations

from datetime import timedelta

from app.jobs.handlers.reconcile_publications import (
    ACCEPTED_POLL_AFTER,
    OUTCOME_UNKNOWN_AFTER,
    SENT_RESUME_AFTER,
)


def test_reconcile_thresholds() -> None:
    assert SENT_RESUME_AFTER == timedelta(minutes=3)
    assert ACCEPTED_POLL_AFTER == timedelta(minutes=5)
    assert OUTCOME_UNKNOWN_AFTER == timedelta(minutes=30)
