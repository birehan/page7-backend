from __future__ import annotations

from app.integrations.email.ports import EmailMessage


class FakeEmailProvider:
    """Records every message it's asked to send, zero network I/O
    (architecture/06 §5) — the adapter tests override `get_email_provider`
    with, never a monkeypatched vendor SDK.
    """

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)
