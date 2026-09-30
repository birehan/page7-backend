"""ResearchProvider port (architecture/08 §3)."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

EXTRACTION_FIELD_NAMES: tuple[str, ...] = (
    "voice_adjectives",
    "do_list",
    "dont_list",
    "banned_claims",
    "colors",
    "dialect",
    "languages",
    "pillars_suggested",
    "competitors_suggested",
    "logo_url",
)

# Wire-side camelCase names inside proposal.patch.guidelines / top-level.
FIELD_TO_PATCH_KEY: dict[str, str] = {
    "voice_adjectives": "voiceAdjectives",
    "do_list": "doList",
    "dont_list": "dontList",
    "banned_claims": "bannedClaims",
    "colors": "colors",
    "dialect": "dialect",
    "languages": "languages",
    "pillars_suggested": "pillars",
    "competitors_suggested": "competitors",
    "logo_url": "logoUrl",
}

GUIDELINE_FIELDS = frozenset(
    {
        "voice_adjectives",
        "do_list",
        "dont_list",
        "banned_claims",
        "colors",
        "dialect",
        "languages",
    }
)


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

    voice_adjectives: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    do_list: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    dont_list: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    banned_claims: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    colors: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    dialect: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    languages: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    pillars_suggested: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    competitors_suggested: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    logo_url: FieldExtraction = Field(default_factory=lambda: FieldExtraction())
    sources: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


@runtime_checkable
class ResearchProvider(Protocol):
    async def research(self, request: ResearchRequest) -> ExtractionResult: ...
