"""Brand research feature — Phase 8."""

from app.features.brand_research.fakes import FakeResearchProvider
from app.features.brand_research.ports import (
    ExtractionResult,
    FieldExtraction,
    ResearchProvider,
    ResearchRequest,
)
from app.features.brand_research.router import router

__all__ = [
    "ExtractionResult",
    "FakeResearchProvider",
    "FieldExtraction",
    "ResearchProvider",
    "ResearchRequest",
    "router",
]
