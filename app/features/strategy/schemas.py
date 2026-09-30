from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from app.core.schema import CamelModel

Platform = Literal["instagram", "facebook", "tiktok", "snapchat", "whatsapp"]


class StrategyGoalOut(CamelModel):
    id: uuid.UUID
    text: str
    progress: float


class StrategyOut(CamelModel):
    id: uuid.UUID
    goals: list[StrategyGoalOut]
    cadence: dict[str, float]
    updated_at: datetime


class UpdateStrategyBody(CamelModel):
    goals: list[StrategyGoalOut] | None = None
    cadence: dict[str, float] | None = None
