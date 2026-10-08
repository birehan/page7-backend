"""Pydantic request/response schemas for /visuals/* (Phase 11)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class GenerateVisualsBody(BaseModel):
    """Generate request. Callers: visuals router → service. User: plan generate-ui quality."""

    model_config = ConfigDict(populate_by_name=True)

    brand_id: UUID = Field(alias="brandId")
    prompt: str = Field(min_length=3, max_length=1000)
    style: Literal["photo", "flat", "three-d", "minimal", "saudi-modern", "poster"]
    aspect: Literal["square", "portrait", "vertical", "landscape"]
    count: int = Field(default=4, ge=1, le=4)
    use_brand_colors: bool = Field(default=False, alias="useBrandColors")
    use_brand_logo: bool = Field(default=False, alias="useBrandLogo")
    headline: str | None = Field(default=None, max_length=200)
    reference_media_id: UUID | None = Field(default=None, alias="referenceMediaId")
    seed: int | None = Field(default=None, alias="seed")
    # draft=Flux Flash, standard=Flux Pro / Ideogram, premium=GPT Image
    quality: Literal["draft", "standard", "premium"] = Field(default="standard")


class KeepVisualBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    brand_id: UUID = Field(alias="brandId")
    url: str
    prompt: str
    style: Literal["photo", "flat", "three-d", "minimal", "saudi-modern", "poster"]
    alt_ar: str = Field(alias="altAr")
    alt_en: str = Field(alias="altEn")
    decision_id: str | None = Field(default=None, alias="decisionId")


class RenderTemplateBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    brand_id: UUID = Field(alias="brandId")
    template_id: str = Field(alias="templateId", min_length=1)
    headline: str = Field(min_length=1)
    subline: str | None = None
    aspect: Literal["square", "portrait", "vertical", "landscape"]
    asset_id: UUID = Field(alias="assetId")
