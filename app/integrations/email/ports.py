from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class EmailMessage(BaseModel):
    """architecture/06 §3: typed request objects, never a raw dict.
    `idempotency_key` is a stable business id (an invitation id, a
    reset-token id) so a retried send never double-delivers.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    to: str
    subject: str
    html: str
    text: str
    idempotency_key: str


@runtime_checkable
class EmailProvider(Protocol):
    async def send(self, message: EmailMessage) -> None: ...
