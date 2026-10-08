"""Exact platform pixel canvases for Instagram / Facebook (architecture/09 v2)."""

from __future__ import annotations

from typing import Any

# Target output pixels — match composer preview (platforms.ts).
ASPECT_PIXELS: dict[str, tuple[int, int]] = {
    "square": (1080, 1080),
    "portrait": (1080, 1350),  # IG feed 4:5
    "vertical": (1080, 1920),  # Stories / Reels cover
    "landscape": (1920, 1080),
}

# Fal accepts either named enums or {width, height}. We always send explicit pixels.
AspectSize = dict[str, int]  # {"width": int, "height": int}


def size_for_aspect(aspect: str) -> AspectSize:
    dims = ASPECT_PIXELS.get(aspect)
    if dims is None:
        raise KeyError(aspect)
    width, height = dims
    return {"width": width, "height": height}


def default_aspect_map() -> dict[str, AspectSize]:
    return {name: size_for_aspect(name) for name in ASPECT_PIXELS}


def parse_image_size(image_size: str | dict[str, Any]) -> tuple[int, int]:
    """Resolve fal image_size (enum string or {width,height}) to pixels."""
    if isinstance(image_size, dict):
        width = image_size.get("width")
        height = image_size.get("height")
        if isinstance(width, int) and isinstance(height, int):
            return width, height
        raise ValueError(f"invalid image_size dict: {image_size!r}")

    # Legacy enum names still supported for older jobs / tests.
    legacy: dict[str, tuple[int, int]] = {
        "square_hd": (1024, 1024),
        "portrait_4_3": (768, 1024),
        "portrait_16_9": (576, 1024),
        "landscape_16_9": (1024, 576),
        "square": (1080, 1080),
        "portrait": (1080, 1350),
        "vertical": (1080, 1920),
        "landscape": (1920, 1080),
    }
    if image_size in legacy:
        return legacy[image_size]
    if "x" in image_size:
        left, _, right = image_size.partition("x")
        try:
            return int(left), int(right)
        except ValueError:
            pass
    return 1024, 1024


def fal_image_size_arg(image_size: str | dict[str, Any]) -> str | dict[str, int]:
    """Value to pass to fal_client — prefer explicit width/height."""
    if isinstance(image_size, dict):
        width, height = parse_image_size(image_size)
        return {"width": width, "height": height}
    if image_size in ASPECT_PIXELS:
        width, height = parse_image_size(image_size)
        return {"width": width, "height": height}
    # Named fal enums (square_hd, …) pass through unchanged.
    return image_size
