from __future__ import annotations

import uuid
from datetime import datetime

from app.core.pagination import Page
from app.core.schema import CamelModel


class NotificationOut(CamelModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    type: str
    message_key: str
    params: dict[str, object]
    target_href: str
    actor_id: str | None = None
    read_at: datetime | None = None
    created_at: datetime


NotificationPage = Page[NotificationOut]
