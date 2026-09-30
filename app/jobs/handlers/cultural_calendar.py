"""Yearly cultural-calendar catalog extension."""

from __future__ import annotations

from typing import Any

from app.db.session import get_session_factory
from app.features.cultural_events import extend_catalog


async def handle_extend_catalog(payload: dict[str, Any]) -> None:  # noqa: ARG001
    async with get_session_factory()() as session:
        inserted = await extend_catalog(session)
        await session.commit()
    # Structured logging happens in the worker runner; keep handler quiet.
    _ = inserted
