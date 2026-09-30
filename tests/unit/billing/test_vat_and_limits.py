"""Unit tests for billing VAT arithmetic and limit comparisons."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from app.features.billing.repository import (
    compute_vat,
    is_month_start,
    next_month_start,
    period_month_of,
)


def test_compute_vat_fifteen_percent() -> None:
    vat, total = compute_vat(Decimal("1499.00"), vat_rate=Decimal("0.1500"))
    assert vat == Decimal("224.85")
    assert total == Decimal("1723.85")
    assert total == Decimal("1499.00") + vat


def test_compute_vat_zero_subtotal() -> None:
    vat, total = compute_vat(Decimal("0.00"), vat_rate=Decimal("0.1500"))
    assert vat == Decimal("0.00")
    assert total == Decimal("0.00")


def test_period_month_of() -> None:
    instant = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)
    assert period_month_of(instant).isoformat() == "2026-09-01"


def test_is_month_start() -> None:
    assert is_month_start(datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC))
    assert not is_month_start(datetime(2026, 9, 1, 0, 0, 1, tzinfo=UTC))
    assert not is_month_start(datetime(2026, 9, 2, 0, 0, 0, tzinfo=UTC))


def test_next_month_start_rolls_year() -> None:
    assert next_month_start(datetime(2026, 12, 5, tzinfo=UTC)) == datetime(
        2027, 1, 1, 0, 0, 0, tzinfo=UTC
    )
