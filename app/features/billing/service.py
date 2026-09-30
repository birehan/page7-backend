"""Billing service — subscription, invoices, plan change, usage, enforcement."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.core.time import utc_now
from app.features import audit, notifications, organizations
from app.features.billing import repository
from app.features.billing.models import Invoice, Subscription
from app.features.billing.schemas import (
    AiUsageOut,
    InvoiceOut,
    SubscriptionOut,
    WireInvoiceStatus,
    WirePlan,
    WireSubscriptionStatus,
    money_to_float,
    plan_to_db,
    plan_to_wire,
)

logger = structlog.get_logger(__name__)

_USAGE_THRESHOLDS = (80, 100)


def subscription_to_out(row: Subscription) -> SubscriptionOut:
    status = row.status
    if status == "canceled":
        # Wire schema has no canceled — should not be returned by live queries.
        raise ApiError("NOT_FOUND", "Subscription not found", status_code=404)
    return SubscriptionOut(
        plan=plan_to_wire(row.plan_code),
        price_monthly=money_to_float(row.price_monthly_sar),
        renews_at=row.current_period_end,
        status=cast(WireSubscriptionStatus, status),
    )


def invoice_to_out(row: Invoice) -> InvoiceOut:
    if row.status not in {"pending", "paid", "failed"}:
        raise ValueError(f"invoice {row.number} has non-wire status {row.status}")
    return InvoiceOut(
        id=row.id,
        number=row.number,
        amount=money_to_float(row.amount_subtotal),
        vat_amount=money_to_float(row.vat_amount),
        total=money_to_float(row.total),
        status=cast(WireInvoiceStatus, row.status),
        issued_at=row.issued_at,
    )


async def get_subscription(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> SubscriptionOut:
    row = await repository.get_live_subscription(
        session, organization_id=organization_id
    )
    if row is None:
        raise ApiError("NOT_FOUND", "Subscription not found", status_code=404)
    return subscription_to_out(row)


async def list_invoices(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> list[InvoiceOut]:
    rows = await repository.list_invoices(session, organization_id=organization_id)
    return [invoice_to_out(row) for row in rows]


async def get_ai_usage(
    session: AsyncSession, *, organization_id: uuid.UUID, since: datetime
) -> AiUsageOut:
    if since.tzinfo is None:
        raise ApiError(
            "VALIDATION",
            "since must be a timezone-aware ISO instant",
            status_code=422,
        )
    since_utc = since.astimezone(UTC)
    if repository.is_month_start(since_utc):
        period = repository.period_month_of(since_utc)
        used = await repository.get_usage_generations(
            session, organization_id=organization_id, period_month=period
        )
    else:
        used = await repository.count_ai_decisions_since(
            session, organization_id=organization_id, since=since_utc
        )
    return AiUsageOut(used=used)


async def issue_invoice(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    subscription_id: uuid.UUID,
    subtotal: Decimal,
    period_start: datetime,
    period_end: datetime,
    settings: Settings | None = None,
) -> Invoice:
    """Allocate a gapless invoice number and insert the row in one transaction beat."""
    cfg = settings or get_settings()
    vat_rate = Decimal(str(cfg.billing.default_vat_rate))
    vat_amount, total = repository.compute_vat(subtotal, vat_rate=vat_rate)

    org = await organizations.get_organization(session, organization_id)
    now = utc_now()
    number = await repository.next_invoice_number(session, year=now.year)

    invoice = await repository.insert_invoice(
        session,
        organization_id=organization_id,
        subscription_id=subscription_id,
        number=number,
        status="pending",
        amount_subtotal=subtotal,
        vat_rate=vat_rate,
        vat_amount=vat_amount,
        total=total,
        seller_vat_number=cfg.billing.seller_vat_number,
        buyer_vat_number=org.vat_number,
        period_start=period_start.date(),
        period_end=period_end.date(),
        issued_at=now,
        due_at=now + timedelta(days=14),
    )
    logger.info(
        "billing_invoice_issued",
        organization_id=str(organization_id),
        invoice_number=number,
        plan_code=None,
        total=str(total),
    )
    return invoice


async def change_plan(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    plan: WirePlan,
    actor_user_id: uuid.UUID,
    actor_name: str,
    settings: Settings | None = None,
) -> SubscriptionOut:
    cfg = settings or get_settings()
    plan_code = plan_to_db(plan)
    target = await repository.get_active_plan(session, plan_code)
    if target is None:
        raise ApiError("VALIDATION", f"Unknown or inactive plan: {plan}", status_code=422)

    sub = await repository.get_live_subscription(
        session, organization_id=organization_id
    )
    if sub is None:
        raise ApiError("NOT_FOUND", "Subscription not found", status_code=404)

    if sub.plan_code == plan_code:
        return subscription_to_out(sub)

    previous = sub.plan_code
    sub.plan_code = plan_code
    sub.price_monthly_sar = target.price_monthly_sar
    # Stay on the current period window; renew job issues the next invoice later.
    await session.flush()

    await issue_invoice(
        session,
        organization_id=organization_id,
        subscription_id=sub.id,
        subtotal=target.price_monthly_sar,
        period_start=sub.current_period_start,
        period_end=sub.current_period_end,
        settings=cfg,
    )

    await audit.record(
        session,
        organization_id=organization_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="billing.plan_changed",
        target_type="subscription",
        target_id=sub.id,
        meta={"plan": plan, "previousPlan": plan_to_wire(previous)},
    )
    logger.info(
        "billing_plan_changed",
        organization_id=str(organization_id),
        plan_code=plan_code,
        previous_plan_code=previous,
    )
    return subscription_to_out(sub)


async def enforce_brand_limit(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> None:
    """Raise 403 PLAN_LIMIT_BRANDS when live brand count is at the plan limit.

    Caller must hold `lock_organization_row` so the count-then-insert is not raced.
    """
    sub = await repository.get_live_subscription(
        session, organization_id=organization_id
    )
    if sub is None:
        raise ApiError("NOT_FOUND", "Subscription not found", status_code=404)
    plan = await repository.get_plan(session, sub.plan_code)
    if plan is None:
        raise ApiError("NOT_FOUND", "Plan not found", status_code=404)

    count = await repository.count_live_brands(
        session, organization_id=organization_id
    )
    if count >= plan.brands_limit:
        raise ApiError(
            "PLAN_LIMIT_BRANDS",
            "This organization has reached its plan's brand limit",
            status_code=403,
            details={
                "plan": plan_to_wire(plan.code),
                "limit": plan.brands_limit,
                "used": count,
            },
        )


async def enforce_ai_credit_limit(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> None:
    """Raise 429 AI_CREDITS_EXHAUSTED when this month's generations are at the plan allowance."""
    sub = await repository.get_live_subscription(
        session, organization_id=organization_id
    )
    if sub is None:
        raise ApiError("NOT_FOUND", "Subscription not found", status_code=404)
    plan = await repository.get_plan(session, sub.plan_code)
    if plan is None:
        raise ApiError("NOT_FOUND", "Plan not found", status_code=404)

    now = utc_now()
    period = repository.period_month_of(now)
    used = await repository.get_usage_generations(
        session, organization_id=organization_id, period_month=period
    )
    if used >= plan.ai_generations_monthly:
        resets_at = repository.next_month_start(
            now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        )
        logger.info(
            "ai_credits_exhausted",
            organization_id=str(organization_id),
            plan_code=plan.code,
            limit=plan.ai_generations_monthly,
            used=used,
        )
        raise ApiError(
            "AI_CREDITS_EXHAUSTED",
            "This organization has used its monthly AI generation allowance",
            status_code=429,
            details={
                "plan": plan_to_wire(plan.code),
                "limit": plan.ai_generations_monthly,
                "used": used,
                "resetsAt": resets_at.isoformat().replace("+00:00", "Z"),
            },
        )


async def renew_due_subscriptions(
    session: AsyncSession, *, settings: Settings | None = None
) -> int:
    """Roll periods forward for every live subscription whose period has ended."""
    cfg = settings or get_settings()
    now = utc_now()
    due = await repository.list_subscriptions_due_for_renewal(session, now=now)
    renewed = 0
    for sub in due:
        period_start = sub.current_period_end
        period_end = period_start + timedelta(days=repository.DEFAULT_PERIOD_DAYS)
        # If cancel_at_period_end, mark canceled and skip invoice.
        if sub.cancel_at_period_end:
            sub.status = "canceled"
            sub.canceled_at = now
            await session.flush()
            continue
        sub.current_period_start = period_start
        sub.current_period_end = period_end
        if sub.status == "trialing":
            sub.status = "active"
            sub.trial_ends_at = None
        await session.flush()
        await issue_invoice(
            session,
            organization_id=sub.organization_id,
            subscription_id=sub.id,
            subtotal=sub.price_monthly_sar,
            period_start=period_start,
            period_end=period_end,
            settings=cfg,
        )
        renewed += 1
    return renewed


async def send_usage_alerts(session: AsyncSession) -> int:
    """Notify admins/owners the first time an org crosses 80% and 100% this month."""
    now = utc_now()
    period = repository.period_month_of(now)
    pairs = await repository.list_live_subscriptions_with_plans(session)
    sent = 0
    for sub, plan in pairs:
        used = await repository.get_usage_generations(
            session, organization_id=sub.organization_id, period_month=period
        )
        if plan.ai_generations_monthly <= 0:
            continue
        pct = int((used * 100) / plan.ai_generations_monthly)
        for threshold in _USAGE_THRESHOLDS:
            if pct < threshold:
                continue
            already = await repository.usage_alert_already_sent(
                session,
                organization_id=sub.organization_id,
                period_month=period,
                threshold=threshold,
            )
            if already:
                continue
            await notifications.fan_out(
                session,
                organization_id=sub.organization_id,
                brand_id=None,
                type="ai_credits_warning",
                message_key="ai_credits_warning",
                params={
                    "threshold": threshold,
                    "used": used,
                    "limit": plan.ai_generations_monthly,
                    "plan": plan_to_wire(plan.code),
                    "periodMonth": period.isoformat(),
                },
                target_href="/settings/billing",
                actor_user_id=None,
                actor_ref="system",
                recipient_user_ids=None,
            )
            # Restrict to admin/owner via role filter on type — register below.
            sent += 1
    return sent
