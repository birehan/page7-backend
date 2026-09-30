"""Daily orphan cleanup for expired, incomplete upload_intents.

Deletes the DB row and the tmp/ R2 object. Primary cleanup path ahead of R2's
2-day lifecycle backstop on the tmp/ prefix (architecture/11 §10).
"""

from __future__ import annotations

from typing import Any

import structlog

from app.core.config import get_settings
from app.core.time import utc_now
from app.db.session import get_session_factory
from app.features.media import repository
from app.integrations.storage import get_object_storage

log = structlog.get_logger(__name__)


async def handle(payload: dict[str, Any]) -> None:
    _ = payload
    settings = get_settings()
    storage = get_object_storage(settings)
    factory = get_session_factory()
    removed = 0
    async with factory() as session:
        intents = await repository.list_expired_incomplete_intents(
            session, now=utc_now()
        )
        for intent in intents:
            try:
                await storage.delete_object("public", intent.r2_key)
            except Exception:
                log.exception(
                    "upload_intent_tmp_delete_failed",
                    upload_intent_id=str(intent.id),
                    r2_key=intent.r2_key,
                )
            await repository.delete_upload_intent(session, intent=intent)
            removed += 1
        await session.commit()
    log.info("upload_intents_cleanup", removed=removed)
