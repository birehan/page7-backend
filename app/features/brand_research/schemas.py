"""Request/response schemas for brand research endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from pydantic import BeforeValidator, HttpUrl

from app.core.schema import CamelModel


def _coerce_http_url(value: object) -> object:
    """Accept bare hostnames the way onboarding does (add https://)."""
    if not isinstance(value, str):
        return value
    trimmed = value.strip()
    if trimmed and not trimmed.lower().startswith(("http://", "https://")):
        return f"https://{trimmed}"
    return trimmed


HttpUrlInput = Annotated[HttpUrl, BeforeValidator(_coerce_http_url)]


class RelearnBrandBody(CamelModel):
    brand_id: uuid.UUID
    url: HttpUrlInput


class GenerateBrandBody(CamelModel):
    brand_id: uuid.UUID
    source_url: HttpUrlInput | None = None
    # Optional onboarding hints (audience, goal, languages, …) merged into research context.
    brand_context: dict[str, str] | None = None


class RunOut(CamelModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    brand_id: uuid.UUID | None = None
    kind: str
    status: str
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: Any = None
    started_at: Any = None
    finished_at: Any = None
