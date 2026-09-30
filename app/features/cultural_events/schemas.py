from __future__ import annotations

import uuid
from datetime import date as Date

from app.core.schema import CamelModel


class CulturalEventOut(CamelModel):
    id: uuid.UUID
    name: str
    name_ar: str
    date: Date
    end_date: Date | None = None
    kind: str
    enabled: bool
