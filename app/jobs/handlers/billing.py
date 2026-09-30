"""Billing maintenance job handlers — renew + usage alerts."""

from __future__ import annotations

from typing import Any

import structlog

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.features import billing

logger = structlog.get_logger(__name__)


async def handle_billing_renew(payload: dict[str, Any]) -> None:  # noqa: ARG001
    settings = get_settings()
    async with get_session_factory()() as session:
        renewed = await billing.renew_due_subscriptions(session, settings=settings)
        await session.commit()
    logger.info("billing_renew_complete", renewed=renewed)


async def handle_billing_usage_alerts(payload: dict[str, Any]) -> None:  # noqa: ARG001
    async with get_session_factory()() as session:
        sent = await billing.send_usage_alerts(session)
        await session.commit()
    logger.info("billing_usage_alerts_complete", sent=sent)
