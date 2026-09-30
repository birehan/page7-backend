"""Billing persistence — plans, subscriptions, invoices, counters."""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.features.billing.models import Invoice, InvoiceCounter, Plan, Subscription

_LIVE_STATUSES = ("trialing", "active", "past_due")
_WIRE_INVOICE_STATUSES = ("pending", "paid", "failed")
DEFAULT_PLAN_CODE = "growth"
DEFAULT_TRIAL_DAYS = 14
DEFAULT_PERIOD_DAYS = 30


async def get_plan(session: AsyncSession, code: str) -> Plan | None:
    return await session.get(Plan, code)


async def get_active_plan(session: AsyncSession, code: str) -> Plan | None:
    plan = await get_plan(session, code)
    if plan is None or not plan.active:
        return None
    return plan


async def get_live_subscription(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> Subscription | None:
    stmt = select(Subscription).where(
        Subscription.organization_id == organization_id,
        Subscription.status.in_(_LIVE_STATUSES),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def create_default_subscription(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    plan_code: str = DEFAULT_PLAN_CODE,
) -> Subscription:
    """Create the Growth trial every new org gets at signup."""
    plan = await get_plan(session, plan_code)
    if plan is None:
        raise LookupError(f"plan {plan_code} not seeded")
    now = utc_now()
    row = Subscription(
        organization_id=organization_id,
        plan_code=plan.code,
        status="trialing",
        price_monthly_sar=plan.price_monthly_sar,
        current_period_start=now,
        current_period_end=now + timedelta(days=DEFAULT_PERIOD_DAYS),
        trial_ends_at=now + timedelta(days=DEFAULT_TRIAL_DAYS),
    )
    session.add(row)
    await session.flush()
    return row


async def count_live_brands(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> int:
    result = await session.execute(
        text(
            """
            SELECT count(*)::int FROM brands
            WHERE organization_id = :org_id AND deleted_at IS NULL
            """
        ),
        {"org_id": organization_id},
    )
    return int(result.scalar_one())


async def get_usage_generations(
    session: AsyncSession, *, organization_id: uuid.UUID, period_month: date
) -> int:
    result = await session.execute(
        text(
            """
            SELECT generations FROM ai_usage_counters
            WHERE organization_id = :org_id AND period_month = :period_month
            """
        ),
        {"org_id": organization_id, "period_month": period_month},
    )
    row = result.scalar_one_or_none()
    return int(row) if row is not None else 0


async def count_ai_decisions_since(
    session: AsyncSession, *, organization_id: uuid.UUID, since: datetime
) -> int:
    result = await session.execute(
        text(
            """
            SELECT count(*)::int FROM ai_decisions
            WHERE organization_id = :org_id AND created_at >= :since
            """
        ),
        {"org_id": organization_id, "since": since},
    )
    return int(result.scalar_one())


async def list_invoices(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> list[Invoice]:
    stmt = (
        select(Invoice)
        .where(
            Invoice.organization_id == organization_id,
            Invoice.status.in_(_WIRE_INVOICE_STATUSES),
        )
        .order_by(Invoice.issued_at.desc(), Invoice.id.desc())
    )
    return list((await session.execute(stmt)).scalars().all())


async def next_invoice_number(session: AsyncSession, *, year: int) -> str:
    """Atomically allocate the next gapless invoice number.

    The UPDATE row-lock on invoice_counters serializes concurrent issuance.
    """
    result = await session.execute(
        text(
            """
            UPDATE invoice_counters
            SET last_number = last_number + 1
            WHERE issuer = 'pgblank'
            RETURNING last_number
            """
        )
    )
    number = result.scalar_one()
    return f"INV-{year}-{int(number):06d}"


async def insert_invoice(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    subscription_id: uuid.UUID | None,
    number: str,
    status: str,
    amount_subtotal: Decimal,
    vat_rate: Decimal,
    vat_amount: Decimal,
    total: Decimal,
    seller_vat_number: str,
    buyer_vat_number: str | None,
    period_start: date,
    period_end: date,
    issued_at: datetime,
    due_at: datetime | None = None,
) -> Invoice:
    row = Invoice(
        organization_id=organization_id,
        subscription_id=subscription_id,
        number=number,
        status=status,
        amount_subtotal=amount_subtotal,
        vat_rate=vat_rate,
        vat_amount=vat_amount,
        total=total,
        seller_vat_number=seller_vat_number,
        buyer_vat_number=buyer_vat_number,
        period_start=period_start,
        period_end=period_end,
        issued_at=issued_at,
        due_at=due_at,
    )
    session.add(row)
    await session.flush()
    return row


async def list_subscriptions_due_for_renewal(
    session: AsyncSession, *, now: datetime
) -> list[Subscription]:
    stmt = select(Subscription).where(
        Subscription.status.in_(_LIVE_STATUSES),
        Subscription.current_period_end <= now,
    )
    return list((await session.execute(stmt)).scalars().all())


async def list_live_subscriptions_with_plans(
    session: AsyncSession,
) -> list[tuple[Subscription, Plan]]:
    stmt = (
        select(Subscription, Plan)
        .join(Plan, Plan.code == Subscription.plan_code)
        .where(Subscription.status.in_(_LIVE_STATUSES))
    )
    rows = (await session.execute(stmt)).all()
    return [(sub, plan) for sub, plan in rows]


async def usage_alert_already_sent(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    period_month: date,
    threshold: int,
) -> bool:
    result = await session.execute(
        text(
            """
            SELECT 1 FROM notifications
            WHERE organization_id = :org_id
              AND type = 'ai_credits_warning'
              AND params->>'periodMonth' = :period_month
              AND params->>'threshold' = :threshold
            LIMIT 1
            """
        ),
        {
            "org_id": organization_id,
            "period_month": period_month.isoformat(),
            "threshold": str(threshold),
        },
    )
    return result.first() is not None


async def ensure_invoice_counter(session: AsyncSession) -> None:
    """Idempotent insert of the global counter — used by tests."""
    existing = await session.get(InvoiceCounter, "pgblank")
    if existing is None:
        session.add(InvoiceCounter(issuer="pgblank", last_number=0))
        await session.flush()


def compute_vat(
    subtotal: Decimal, *, vat_rate: Decimal
) -> tuple[Decimal, Decimal]:
    """Return (vat_amount, total) quantized to 2 decimal places."""
    vat_amount = (subtotal * vat_rate).quantize(Decimal("0.01"))
    total = subtotal + vat_amount
    return vat_amount, total


def period_month_of(instant: datetime) -> date:
    return date(instant.year, instant.month, 1)


def is_month_start(instant: datetime) -> bool:
    """True when `since` is exactly the first instant of a calendar month (UTC)."""
    return (
        instant.day == 1
        and instant.hour == 0
        and instant.minute == 0
        and instant.second == 0
        and instant.microsecond == 0
    )


def next_month_start(instant: datetime) -> datetime:
    """UTC midnight of the first day of the month after `instant`."""
    if instant.month == 12:
        return instant.replace(
            year=instant.year + 1,
            month=1,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    return instant.replace(
        month=instant.month + 1,
        day=1,
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )


async def count_live_subscriptions(session: AsyncSession) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.status.in_(_LIVE_STATUSES))
    )
    return int(result.scalar_one())
