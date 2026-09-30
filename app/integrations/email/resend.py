from __future__ import annotations

import resend
from resend.exceptions import RateLimitError, ResendError

from app.integrations.email.ports import EmailMessage
from app.integrations.errors import ProviderRateLimitedError, ProviderUnavailableError


class ResendEmailProvider:
    """The only file allowed to `import resend` — `backend/pyproject.toml`'s
    import-linter contract names this module as its one exception.
    """

    def __init__(self, *, api_key: str, from_address: str) -> None:
        self._from_address = from_address
        resend.api_key = api_key

    async def send(self, message: EmailMessage) -> None:
        try:
            await resend.Emails.send_async(
                {
                    "from": self._from_address,
                    "to": [message.to],
                    "subject": message.subject,
                    "html": message.html,
                    "text": message.text,
                    "headers": {"Idempotency-Key": message.idempotency_key},
                }
            )
        except RateLimitError as exc:
            raise ProviderRateLimitedError() from exc
        except ResendError as exc:
            raise ProviderUnavailableError(str(exc)) from exc
