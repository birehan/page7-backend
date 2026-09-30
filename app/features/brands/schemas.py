from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.core.schema import CamelModel

Dialect = Literal["gulf", "msa"]
Tone = Literal["friendly", "professional", "playful", "authoritative", "warm"]
EmojiUsage = Literal["none", "sparing", "generous"]
CtaStyle = Literal["direct", "soft", "none"]


class BrandVoiceOut(CamelModel):
    tone: Tone
    emoji_usage: EmojiUsage
    cta_style: CtaStyle
    rules: list[str]


class BrandGuidelinesOut(CamelModel):
    voice_adjectives: list[str]
    do_list: list[str]
    dont_list: list[str]
    banned_claims: list[str]
    colors: list[str]
    dialect: Dialect
    languages: list[Literal["ar", "en"]]
    voice: BrandVoiceOut | None = None


class ContentPillarOut(CamelModel):
    id: uuid.UUID
    name: str
    description: str
    weight: float


class CompetitorOut(CamelModel):
    id: uuid.UUID
    handle: str
    platform: str


class BrandOut(CamelModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    industry: str
    city: str
    website: str | None = None
    logo_url: str | None = None
    guidelines: BrandGuidelinesOut
    pillars: list[ContentPillarOut]
    competitors: list[CompetitorOut]
    version: int
    updated_at: datetime
    created_at: datetime


class CreateBrandBody(CamelModel):
    name: str
    industry: str
    city: str
    website: str | None = None


class GenerateBrandBrainBody(CamelModel):
    name: str
    industry: str
    city: str
    website: str | None = None
    dialect: Dialect | None = None


class IngestLogoBody(CamelModel):
    """Fetch a remote logo through the SSRF-safe client into public storage."""

    source_url: str = Field(min_length=8, max_length=2048)
    expected_version: int = Field(gt=0)


class ContentPillarIn(CamelModel):
    id: uuid.UUID
    name: str
    description: str
    weight: float


class CompetitorIn(CamelModel):
    id: uuid.UUID
    handle: str
    platform: str


class UpdateBrandBody(CamelModel):
    """Full brand document — matches `updateBrandBody = brandSchema` on the wire."""

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    industry: str
    city: str
    website: str | None = None
    logo_url: str | None = None
    guidelines: BrandGuidelinesOut
    pillars: list[ContentPillarIn]
    competitors: list[CompetitorIn]
    version: int = Field(gt=0)
    updated_at: datetime
    created_at: datetime


def guidelines_to_dict(guidelines: BrandGuidelinesOut) -> dict[str, Any]:
    data = guidelines.model_dump(by_alias=True, exclude_none=True)
    return data
