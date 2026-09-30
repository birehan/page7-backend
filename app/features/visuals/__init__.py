"""AI image generation and template registration (Phase 11)."""

from __future__ import annotations

from app.features.visuals.models import ImageGeneration, ImageGenerationOutput
from app.features.visuals.router import router

__all__ = ["ImageGeneration", "ImageGenerationOutput", "router"]
