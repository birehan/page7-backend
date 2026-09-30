"""Publishing schemas — queue shape only; PostOut stays in posts."""

from __future__ import annotations

from typing import Any

from app.core.schema import CamelModel

QUEUE_STATUSES = (
    "approved",
    "scheduled",
    "publishing",
    "published",
    "failed",
)


class PublishingQueueOut(CamelModel):
    """Bare Record<status, Post[]> — PostOut instances filled by the router."""

    approved: list[Any]
    scheduled: list[Any]
    publishing: list[Any]
    published: list[Any]
    failed: list[Any]
