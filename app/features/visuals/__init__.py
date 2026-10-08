"""AI image generation and template registration (Phase 11 / v2).

Keep this package init free of router imports so lab CLIs (imagegen_bakeoff)
can import leaf modules without circular import chains through media/brands.
"""

from __future__ import annotations

from app.features.visuals.models import ImageGeneration, ImageGenerationOutput

__all__ = ["ImageGeneration", "ImageGenerationOutput"]
