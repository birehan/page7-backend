"""ResearchProvider port (architecture/08 §3).

Identity-only extraction: name, industry, description, colors (≤3),
preferred language (ar|en), logo. Voice/pillars/competitors/dialect are
not researched.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

EXTRACTION_FIELD_NAMES: tuple[str, ...] = (
    "name",
    "industry",
    "description",
    "colors",
    "languages",
    "logo_url",
)

# Wire-side camelCase names inside proposal.patch.guidelines / top-level.
FIELD_TO_PATCH_KEY: dict[str, str] = {
    "name": "name",
    "industry": "industry",
    "description": "description",
    "colors": "colors",
    "languages": "languages",
    "logo_url": "logoUrl",
}

GUIDELINE_FIELDS = frozenset(
    {
        "colors",
        "languages",
    }
)

IDENTITY_PATCH_KEYS = frozenset({"name", "industry", "description", "logoUrl", "guidelines"})
IDENTITY_GUIDELINE_KEYS = frozenset({"colors", "languages"})


class ResearchRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_url: str
    brand_context: dict[str, str] = Field(default_factory=dict)


class FieldExtraction(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: Any = None
    confidence: float | None = None
    source_page_urls: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    industry: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    description: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    colors: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    languages: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    logo_url: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    sources: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@runtime_checkable
class ResearchProvider(Protocol):
    async def research(self, request: ResearchRequest) -> ExtractionResult: ...
