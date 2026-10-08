"""Deterministic brand logo overlay + canvas normalize (Pillow).

Callers: jobs/handlers/visuals.py after Fal returns; bakeoff script for variant F.
Does not invent logos — overlays the real brand logo_url bytes.
User asked to implement logo-composite from the production AI image gen plan.
"""

from __future__ import annotations

import io
from typing import Literal

from PIL import Image, ImageDraw

LogoCorner = Literal["bottom_left", "bottom_right", "top_left", "top_right"]


def normalize_canvas(
    image_bytes: bytes,
    *,
    target_width: int,
    target_height: int,
) -> bytes:
    """Center-crop / letterbox-free cover resize to exact platform pixels."""
    with Image.open(io.BytesIO(image_bytes)) as src:
        img = src.convert("RGBA")
        src_w, src_h = img.size
        if src_w <= 0 or src_h <= 0:
            raise ValueError("empty source image")
        scale = max(target_width / src_w, target_height / src_h)
        new_w = max(1, int(round(src_w * scale)))
        new_h = max(1, int(round(src_h * scale)))
        resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
        left = max(0, (new_w - target_width) // 2)
        top = max(0, (new_h - target_height) // 2)
        cropped = resized.crop((left, top, left + target_width, top + target_height))
        out = cropped.convert("RGB")
        buf = io.BytesIO()
        out.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def _looks_like_svg(data: bytes) -> bool:
    head = data.lstrip()[:256].lower()
    return (
        head.startswith(b"<svg")
        or b"<svg" in head
        or b'xmlns="http://www.w3.org/2000/svg"' in head
    )


def rasterize_logo_bytes(logo_bytes: bytes) -> bytes:
    """Ensure logo bytes are Pillow-openable (rasterize SVG when needed).

    Callers: composite_logo / apply_brand_finish (jobs/handlers/visuals).
    User: this is not adding the logo in the generated image.
    """
    if not _looks_like_svg(logo_bytes):
        return logo_bytes
    try:
        import cairosvg  # type: ignore[import-untyped]
    except ImportError as exc:
        raise ValueError(
            "Brand logo is SVG; install cairosvg to composite it, "
            "or upload a PNG/WebP logo"
        ) from exc
    png = cairosvg.svg2png(bytestring=logo_bytes, output_width=512)
    if not isinstance(png, (bytes, bytearray)) or not png:
        raise ValueError("SVG logo rasterization produced empty output")
    return bytes(png)


def composite_logo(
    image_bytes: bytes,
    logo_bytes: bytes,
    *,
    corner: LogoCorner = "bottom_left",
    logo_width_ratio: float = 0.16,
    padding_ratio: float = 0.04,
    plate: bool = True,
) -> bytes:
    """Overlay brand logo in a safe corner. Logo stays pixel-faithful (not AI-mutated)."""
    logo_bytes = rasterize_logo_bytes(logo_bytes)
    with Image.open(io.BytesIO(image_bytes)) as base_src, Image.open(
        io.BytesIO(logo_bytes)
    ) as logo_src:
        base = base_src.convert("RGBA")
        logo = logo_src.convert("RGBA")
        bw, bh = base.size
        if bw <= 0 or bh <= 0:
            raise ValueError("empty base image")

        target_logo_w = max(24, int(bw * logo_width_ratio))
        lw, lh = logo.size
        if lw <= 0 or lh <= 0:
            raise ValueError("empty logo")
        scale = target_logo_w / lw
        logo_resized = logo.resize(
            (max(1, int(round(lw * scale))), max(1, int(round(lh * scale)))),
            Image.Resampling.LANCZOS,
        )
        pad = max(8, int(bw * padding_ratio))
        lw2, lh2 = logo_resized.size

        if corner == "bottom_left":
            x, y = pad, bh - lh2 - pad
        elif corner == "bottom_right":
            x, y = bw - lw2 - pad, bh - lh2 - pad
        elif corner == "top_left":
            x, y = pad, pad
        else:
            x, y = bw - lw2 - pad, pad

        layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
        if plate:
            plate_pad = max(4, pad // 3)
            plate_box = (
                x - plate_pad,
                y - plate_pad,
                x + lw2 + plate_pad,
                y + lh2 + plate_pad,
            )
            draw = ImageDraw.Draw(layer)
            draw.rounded_rectangle(
                plate_box,
                radius=max(4, plate_pad),
                fill=(255, 255, 255, 180),
            )
        layer.paste(logo_resized, (x, y), logo_resized)
        composed = Image.alpha_composite(base, layer).convert("RGB")
        buf = io.BytesIO()
        composed.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


def apply_brand_finish(
    image_bytes: bytes,
    *,
    target_width: int,
    target_height: int,
    logo_bytes: bytes | None = None,
    corner: LogoCorner = "bottom_left",
) -> bytes:
    """Normalize to platform size, then optionally composite the brand logo."""
    normalized = normalize_canvas(
        image_bytes, target_width=target_width, target_height=target_height
    )
    if logo_bytes is None:
        return normalized
    return composite_logo(normalized, logo_bytes, corner=corner)
