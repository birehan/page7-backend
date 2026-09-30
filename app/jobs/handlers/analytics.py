"""Job handlers for analytics (Phase 12 Stage 4)."""

from __future__ import annotations

from typing import Any

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.features import analytics as analytics_feature
from app.integrations.llm import get_llm_router

log = __import__("structlog").get_logger(__name__)


async def handle_analytics_sync(payload: dict[str, Any]) -> None:
    credential_id = None
    raw = payload.get("credential_id")
    if isinstance(raw, str) and raw:
        import uuid

        try:
            credential_id = uuid.UUID(raw)
        except ValueError:
            credential_id = None
    factory = get_session_factory()
    async with factory() as session:
        await analytics_feature.run_analytics_sync(
            session, credential_id=credential_id
        )
        await session.commit()


async def handle_analytics_weekly_insights(payload: dict[str, Any]) -> None:
    _ = payload
    settings = get_settings()
    router = get_llm_router(settings)
    factory = get_session_factory()
    async with factory() as session:
        count = await analytics_feature.run_weekly_insights(
            session, settings=settings, router=router
        )
        await session.commit()
    log.info("analytics_weekly_insights.done", inserted=count)
