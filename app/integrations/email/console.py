from __future__ import annotations

import structlog

from app.integrations.email.ports import EmailMessage

logger = structlog.get_logger(__name__)


class ConsoleEmailProvider:
    """The dev/test adapter — logs the rendered email instead of sending it.
    Selected automatically under `development`/`testing` (architecture/06 §6:
    "the one adapter whose entire purpose is local dev/test, not a second
    vendor"). Logs under the `email` key, not `to`, so the redaction
    processor's email-hashing branch (core/logging.py) actually applies to it.
    """

    async def send(self, message: EmailMessage) -> None:
        logger.info(
            "email_send_console",
            email=message.to,
            subject=message.subject,
            idempotency_key=message.idempotency_key,
        )
