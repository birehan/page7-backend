from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.core.pagination import Page
from app.core.schema import CamelModel

MediaKind = Literal["image", "video", "template"]
MediaSource = Literal["upload", "stock", "generated", "template"]


class FocalPointIn(CamelModel):
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class MediaAttributionOut(CamelModel):
    provider: str
    author: str | None = None
    author_url: str | None = None
    source_url: str | None = None
    license: str | None = None


class TemplateParamsOut(CamelModel):
    template_id: str
    headline: str
    subline: str | None = None
    aspect: Literal["square", "portrait", "vertical", "landscape"]


class GenerationParamsOut(CamelModel):
    prompt: str
    style: str
    decision_id: str | None = None
    seed: int | None = None


class MediaLibraryItemOut(CamelModel):
    id: str
    brand_id: str
    kind: MediaKind
    url: str
    width: int
    height: int
    alt_ar: str
    alt_en: str
    tags: list[str]
    created_at: datetime
    ephemeral: bool | None = None
    source: MediaSource | None = None
    attribution: MediaAttributionOut | None = None
    template: TemplateParamsOut | None = None
    generation: GenerationParamsOut | None = None
    focal_point: FocalPointIn | None = None
    duration_seconds: float | None = None
    poster_url: str | None = None


MediaLibraryPage = Page[MediaLibraryItemOut]


class CreateUploadUrlBody(CamelModel):
    filename: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    size: int = Field(gt=0)


class CreateUploadUrlResponse(CamelModel):
    upload_url: str
    asset_id: str
    headers: dict[str, str] | None = None


class UpdateMediaAltBody(CamelModel):
    alt_ar: str
    alt_en: str


class UpdateFocalPointBody(CamelModel):
    focal_point: FocalPointIn


class StockPhotoOut(CamelModel):
    id: str
    thumb_url: str
    full_url: str
    width: int
    height: int
    description: str
    attribution: MediaAttributionOut


class StockSearchResponse(CamelModel):
    items: list[StockPhotoOut]
    next_page: int | None = None


class ImportStockBody(CamelModel):
    stock_id: str = Field(min_length=1)
    full_url: str
    alt_ar: str
    alt_en: str
    attribution: MediaAttributionOut


# Re-export for type checkers / callers that need the raw attribution dict shape.
AttributionDict = dict[str, Any]
