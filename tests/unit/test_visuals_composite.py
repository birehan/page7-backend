"""Unit tests for brand logo composite + canvas normalize.

Callers: pytest. User: implement logo-composite from AI image gen plan.
"""

from __future__ import annotations

import io

from PIL import Image

from app.features.visuals.aspects import parse_image_size, size_for_aspect
from app.features.visuals.composite import apply_brand_finish, normalize_canvas


def _solid_png(width: int, height: int, color: tuple[int, int, int] = (20, 40, 60)) -> bytes:
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_size_for_aspect_ig_feed_is_4_5() -> None:
    size = size_for_aspect("portrait")
    assert size == {"width": 1080, "height": 1350}
    assert parse_image_size(size) == (1080, 1350)


def test_normalize_canvas_exact_pixels() -> None:
    raw = _solid_png(800, 600)
    out = normalize_canvas(raw, target_width=1080, target_height=1350)
    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (1080, 1350)


def test_apply_brand_finish_composites_logo() -> None:
    base = _solid_png(1080, 1350, (30, 30, 30))
    logo = _solid_png(200, 80, (255, 0, 0))
    out = apply_brand_finish(
        base,
        target_width=1080,
        target_height=1350,
        logo_bytes=logo,
        corner="bottom_left",
    )
    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (1080, 1350)
        px = img.getpixel((60, 1280))
        assert px != (30, 30, 30)


def test_apply_brand_finish_rasterizes_svg_logo() -> None:
    # Callers: pytest (this file). Existing composite tests cover PNG; this
    # covers SVG→PNG via cairosvg. User: "this is not adding the logo in the
    # generated image".
    base = _solid_png(1080, 1350, (30, 30, 30))
    svg = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="80">'
        b'<rect width="200" height="80" fill="#ff0000"/></svg>'
    )
    out = apply_brand_finish(
        base,
        target_width=1080,
        target_height=1350,
        logo_bytes=svg,
        corner="bottom_left",
    )
    with Image.open(io.BytesIO(out)) as img:
        assert img.size == (1080, 1350)
        px = img.getpixel((60, 1280))
        assert px != (30, 30, 30)
