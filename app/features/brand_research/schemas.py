"""Request/response schemas for brand research endpoints."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import HttpUrl

from app.core.schema import CamelModel


class RelearnBrandBody(CamelModel):
    brand_id: uuid.UUID
    url: HttpUrl


class GenerateBrandBody(CamelModel):
    brand_id: uuid.UUID
    source_url: HttpUrl | None = None
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
