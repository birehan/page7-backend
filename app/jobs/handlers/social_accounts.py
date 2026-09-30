"""Job handlers for social_accounts (Phase 9)."""

from __future__ import annotations

from typing import Any

from app.db.session import get_session_factory
from app.features.social_accounts import tasks
from app.jobs.errors import TerminalError

log = __import__("structlog").get_logger(__name__)


async def handle_zernio_health_poll(payload: dict[str, Any]) -> None:
    _ = payload
    factory = get_session_factory()
    async with factory() as session:
        await tasks.run_zernio_health_poll(session)
        await session.commit()


async def handle_process_webhook_event(payload: dict[str, Any]) -> None:
    try:
        webhook_event_id = int(payload["webhook_event_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TerminalError("WEBHOOK_BAD_PAYLOAD") from exc
    alias = payload.get("alias")
    alias_str = str(alias) if alias is not None else None

    factory = get_session_factory()
    async with factory() as session:
        await tasks.process_webhook_event(
            session, webhook_event_id=webhook_event_id, alias=alias_str
        )
        await session.commit()
