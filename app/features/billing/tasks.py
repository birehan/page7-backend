"""Billing background task helpers — called from job handlers."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.features.billing import service as billing


async def renew_subscriptions(session: AsyncSession, *, settings: Settings) -> int:
    return await billing.renew_due_subscriptions(session, settings=settings)


async def send_usage_alerts(session: AsyncSession) -> int:
    return await billing.send_usage_alerts(session)
