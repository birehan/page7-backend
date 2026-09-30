from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import Field

from app.core.schema import CamelModel
from app.features.posts.schemas import PostVariantIn

CaptionIntent = Literal[
    "generate",
    "regenerate",
    "shorter",
    "longer",
    "add_cta",
    "formal",
    "casual",
    "translate_to_en",
    "translate_to_ar",
    "hashtags",
    "hook",
]
Platform = Literal["instagram", "facebook", "tiktok", "snapchat", "whatsapp"]
Dialect = Literal["gulf", "msa"]


class CaptionSeedIn(CamelModel):
    ar: str | None = None
    en: str | None = None
    hashtags: list[str] | None = None


class GenerateCaptionsBody(CamelModel):
    brand_id: uuid.UUID
    platforms: list[Platform] = Field(min_length=1)
    intent: CaptionIntent
    dialect: Dialect
    pillar_id: uuid.UUID | None = None
    cultural_event_id: uuid.UUID | None = None
    count: int = Field(default=3, ge=1, le=5)
    seed: CaptionSeedIn | None = None
    variant_index: int | None = Field(default=None, ge=0)


class GeneratePlanBody(CamelModel):
    brand_id: uuid.UUID
    from_: date = Field(alias="from")
    to: date
    cadence: dict[str, int]
    only_gaps: bool
    target_count: int | None = Field(default=None, ge=1, le=60)


class PlanDraftItemIn(CamelModel):
    id: uuid.UUID
    date_key: str
    platform: Platform
    scheduled_at: datetime
    pillar_id: uuid.UUID
    cultural_event_id: uuid.UUID | None = None
    variants: list[PostVariantIn]


class CommitPlanBody(CamelModel):
    brand_id: uuid.UUID
    decision_id: str | None = None
    items: list[PlanDraftItemIn] = Field(min_length=1)


class RegenerateStrategyBody(CamelModel):
    brand_id: uuid.UUID


class AiFeedbackBody(CamelModel):
    decision_id: str
    variant_index: int = Field(ge=0)
    rating: Literal["up", "down"]


class AltTextBody(CamelModel):
    brand_id: uuid.UUID
    media_url: str
    caption: str | None = None


class AltTextResponse(CamelModel):
    alt_ar: str
    alt_en: str
