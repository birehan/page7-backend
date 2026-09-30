"""Billing tables — architecture/02 §15 (Phase 13)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, CreatedAtMixin, TenantMixin, TimestampMixin, UUIDPrimaryKeyMixin

_SUBSCRIPTION_STATUSES = ("trialing", "active", "past_due", "canceled")
_INVOICE_STATUSES = ("draft", "pending", "paid", "failed", "void", "refunded")
_WIRE_INVOICE_STATUSES = frozenset({"pending", "paid", "failed"})


class Plan(Base):
    """Lookup table — natural text PK (starter/growth/agency), not uuidv7."""

    __tablename__ = "plans"

    code: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    price_monthly_sar: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    ai_generations_monthly: Mapped[int] = mapped_column(Integer)
    brands_limit: Mapped[int] = mapped_column(Integer)
    seats_limit: Mapped[int] = mapped_column(Integer)
    channels_limit: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


class Subscription(Base, UUIDPrimaryKeyMixin, TimestampMixin, TenantMixin):
    """One live subscription per org (partial unique on live statuses)."""

    __tablename__ = "subscriptions"

    plan_code: Mapped[str] = mapped_column(ForeignKey("plans.code", ondelete="RESTRICT"))
    status: Mapped[str]
    price_monthly_sar: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    current_period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    trial_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false")
    )
    canceled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    psp: Mapped[str | None] = mapped_column(default=None)
    psp_customer_id: Mapped[str | None] = mapped_column(default=None)
    psp_subscription_id: Mapped[str | None] = mapped_column(default=None)
    payment_method: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)

    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _SUBSCRIPTION_STATUSES) + ")",
            name="status_valid",
        ),
        Index(
            "ux_subscription_current",
            "organization_id",
            unique=True,
            postgresql_where=text("status IN ('trialing', 'active', 'past_due')"),
        ),
    )


class Invoice(Base, UUIDPrimaryKeyMixin, CreatedAtMixin, TenantMixin):
    """Append-only sales invoice ledger — never deleted."""

    __tablename__ = "invoices"

    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="SET NULL"), default=None
    )
    number: Mapped[str]
    status: Mapped[str]
    currency: Mapped[str] = mapped_column(CHAR(3), server_default="SAR")
    amount_subtotal: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    vat_rate: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), server_default=text("0.1500")
    )
    vat_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    total: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    seller_vat_number: Mapped[str]
    buyer_vat_number: Mapped[str | None] = mapped_column(default=None)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    psp_payment_id: Mapped[str | None] = mapped_column(default=None)
    psp_raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, default=None)
    pdf_r2_key: Mapped[str | None] = mapped_column(default=None)
    zatca_uuid: Mapped[str | None] = mapped_column(default=None)
    zatca_hash: Mapped[str | None] = mapped_column(default=None)
    qr_payload: Mapped[str | None] = mapped_column(default=None)

    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{v}'" for v in _INVOICE_STATUSES) + ")",
            name="status_valid",
        ),
        CheckConstraint("currency = 'SAR'", name="currency_sar"),
        CheckConstraint(
            "total = amount_subtotal + vat_amount", name="total_equals_parts"
        ),
        UniqueConstraint("number", name="uq_invoices_number"),
        Index(
            "ix_invoices_org_issued",
            "organization_id",
            text("issued_at DESC"),
        ),
    )


class InvoiceCounter(Base):
    """Global gapless invoice number sequence — one row, issuer='pgblank'."""

    __tablename__ = "invoice_counters"

    issuer: Mapped[str] = mapped_column(primary_key=True)
    last_number: Mapped[int] = mapped_column(BigInteger)
