"""Normalize Firecrawl / dembrandt branding payloads into a stable measured shape.

Shared by the crawler adapter, brand evidence aggregation, finalize override, and
the verify CLI — so production and the timed probe apply the same rules.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .scrub import scrub_free_text

_HEX6 = re.compile(r"^#?[0-9A-Fa-f]{6}$")
_RGB = re.compile(r"^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", re.IGNORECASE)
_COLOR_ROLE_ORDER = (
    "primary",
    "secondary",
    "accent",
    "background",
    "textPrimary",
    "textSecondary",
    "link",
)
_MAX_COLORS = 12
_MAX_FONTS = 8
_MAX_DATA_URI_LEN = 2_048


@dataclass(frozen=True, slots=True)
class MeasuredBranding:
    """Stable subset we persist and prefer over LLM-guessed visuals."""

    colors: tuple[str, ...] = ()
    logo_url: str | None = None
    color_scheme: str | None = None
    fonts: tuple[str, ...] = ()
    raw: dict[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        """JSON-plain form for `brand_sources.branding` / CrawlResult.branding."""
        out: dict[str, object] = {
            "colors": list(self.colors),
            "fonts": list(self.fonts),
        }
        if self.logo_url:
            out["logo_url"] = self.logo_url
        if self.color_scheme:
            out["color_scheme"] = self.color_scheme
        return out

    @property
    def empty(self) -> bool:
        return not self.colors and not self.logo_url


def _as_hex(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not _HEX6.match(cleaned):
        return None
    if not cleaned.startswith("#"):
        cleaned = f"#{cleaned}"
    return cleaned.upper()


def _rgb_to_hex(value: str) -> str | None:
    match = _RGB.match(value.strip())
    if not match:
        return None
    r, g, b = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return f"#{r:02X}{g:02X}{b:02X}"


def _any_color(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return _as_hex(value) or _rgb_to_hex(value)


def _is_usable_asset_url(value: str) -> bool:
    cleaned = value.strip()
    if not cleaned or cleaned == "data:,":
        return False
    if cleaned.startswith("data:") and len(cleaned) > _MAX_DATA_URI_LEN:
        return False
    # dembrandt sometimes puts the page URL on `logo.url` for inline SVGs.
    if cleaned.startswith(("http://", "https://")):
        parsed = urlparse(cleaned)
        path = (parsed.path or "").rstrip("/")
        if path == "" and not parsed.query and not parsed.fragment:
            return False
    return True


def _collect_colors(raw_colors: object) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(candidate: object) -> None:
        hex_value = _as_hex(candidate)
        if hex_value is None or hex_value in seen:
            return
        seen.add(hex_value)
        found.append(hex_value)

    if isinstance(raw_colors, dict):
        for role in _COLOR_ROLE_ORDER:
            if role in raw_colors:
                add(raw_colors[role])
        for key, value in raw_colors.items():
            if key in _COLOR_ROLE_ORDER:
                continue
            add(value)
    elif isinstance(raw_colors, list):
        for item in raw_colors:
            if isinstance(item, dict):
                add(item.get("hex") or item.get("value") or item.get("color"))
            else:
                add(item)
    return found


def _pick_logo(raw: dict[str, object]) -> str | None:
    images = raw.get("images")
    if isinstance(images, dict):
        for key in ("logo", "favicon", "ogImage"):
            value = images.get(key)
            if isinstance(value, str) and value.strip():
                # Skip huge data-URI SVGs for storage; favicon/og URL is fine.
                if value.startswith("data:") and len(value) > _MAX_DATA_URI_LEN:
                    continue
                return scrub_free_text(value.strip())
    top = raw.get("logo")
    if isinstance(top, str) and top.strip() and not (
        top.startswith("data:") and len(top) > _MAX_DATA_URI_LEN
    ):
        return scrub_free_text(top.strip())
    return None


def _collect_fonts(raw: dict[str, object]) -> list[str]:
    fonts: list[str] = []
    seen: set[str] = set()

    def add(name: object) -> None:
        if not isinstance(name, str):
            return
        cleaned = scrub_free_text(name.strip())
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        fonts.append(cleaned)

    raw_fonts = raw.get("fonts")
    if isinstance(raw_fonts, list):
        for item in raw_fonts:
            if isinstance(item, dict):
                add(item.get("family") or item.get("name"))
            else:
                add(item)
    typography = raw.get("typography")
    if isinstance(typography, dict):
        families = typography.get("fontFamilies")
        if isinstance(families, dict):
            for value in families.values():
                add(value)
    return fonts


def normalize_branding(raw: object) -> MeasuredBranding:
    """Turn Firecrawl's nested branding object into MeasuredBranding.

    Missing or empty input yields an empty MeasuredBranding (safe degrade).
    """
    if not isinstance(raw, dict) or not raw:
        return MeasuredBranding()

    colors = _collect_colors(raw.get("colors"))
    logo_url = _pick_logo(raw)
    scheme = raw.get("colorScheme") or raw.get("color_scheme")
    color_scheme = scrub_free_text(str(scheme)) if scheme else None
    fonts = _collect_fonts(raw)

    return MeasuredBranding(
        colors=tuple(colors),
        logo_url=logo_url,
        color_scheme=color_scheme or None,
        fonts=tuple(fonts),
        raw=raw,
    )


def _push_dembrandt_color_list(target: list[str], seen: set[str], list_value: object) -> None:
    if not isinstance(list_value, list):
        return
    for item in list_value:
        hex_value: str | None = None
        if isinstance(item, str):
            hex_value = _any_color(item)
        elif isinstance(item, dict):
            hex_value = (
                _any_color(item.get("normalized"))
                or _any_color(item.get("hex"))
                or _any_color(item.get("color"))
            )
        if hex_value and hex_value not in seen:
            seen.add(hex_value)
            target.append(hex_value)


def _collect_dembrandt_colors(raw: dict[str, object]) -> list[str]:
    colors: list[str] = []
    seen: set[str] = set()
    colors_node = raw.get("colors")
    if isinstance(colors_node, dict):
        _push_dembrandt_color_list(colors, seen, colors_node.get("palette"))
        _push_dembrandt_color_list(colors, seen, colors_node.get("detected"))
        _push_dembrandt_color_list(colors, seen, colors_node.get("_raw"))
        semantic = colors_node.get("semantic")
        if isinstance(semantic, dict):
            for value in semantic.values():
                hex_value = _any_color(value if isinstance(value, str) else None)
                if isinstance(value, dict):
                    hex_value = (
                        _any_color(value.get("normalized"))
                        or _any_color(value.get("hex"))
                        or _any_color(value.get("color"))
                    )
                if hex_value and hex_value not in seen:
                    seen.add(hex_value)
                    colors.append(hex_value)
    else:
        for hex_value in _collect_colors(colors_node):
            if hex_value not in seen:
                seen.add(hex_value)
                colors.append(hex_value)
    return colors[:_MAX_COLORS]


def _collect_dembrandt_fonts(raw: dict[str, object]) -> list[str]:
    fonts: list[str] = []
    seen: set[str] = set()

    def add(name: object) -> None:
        if not isinstance(name, str):
            return
        cleaned = scrub_free_text(name.strip())
        if not cleaned or cleaned in seen:
            return
        seen.add(cleaned)
        fonts.append(cleaned)

    typography = raw.get("typography")
    if isinstance(typography, dict):
        styles = typography.get("styles")
        if isinstance(styles, list):
            for style in styles:
                if isinstance(style, dict):
                    add(style.get("fontFamily") or style.get("family"))
        families = typography.get("families")
        if isinstance(families, list):
            for family in families:
                if isinstance(family, str):
                    add(family)
                elif isinstance(family, dict):
                    add(family.get("name") or family.get("family"))
    return fonts[:_MAX_FONTS]


def _pick_dembrandt_logo(raw: dict[str, object]) -> str | None:
    candidates: list[str] = []

    logo = raw.get("logo")
    if isinstance(logo, str) and _is_usable_asset_url(logo):
        candidates.append(logo.strip())
    elif isinstance(logo, dict):
        for key in ("url", "src", "href", "dataUrl", "dataUri"):
            value = logo.get(key)
            if isinstance(value, str) and _is_usable_asset_url(value):
                candidates.append(value.strip())

    favicons = raw.get("favicons")
    if isinstance(favicons, list):
        for fav in favicons:
            if isinstance(fav, str) and _is_usable_asset_url(fav):
                candidates.append(fav.strip())
            elif isinstance(fav, dict):
                value = fav.get("url") or fav.get("href") or fav.get("src")
                if isinstance(value, str) and _is_usable_asset_url(value):
                    candidates.append(value.strip())

    for candidate in candidates:
        return scrub_free_text(candidate)
    return None


def normalize_dembrandt(raw: object) -> MeasuredBranding:
    """Turn dembrandt CLI JSON into MeasuredBranding.

    Missing or empty input yields an empty MeasuredBranding (safe degrade).
    """
    if not isinstance(raw, dict) or not raw:
        return MeasuredBranding()

    return MeasuredBranding(
        colors=tuple(_collect_dembrandt_colors(raw)),
        logo_url=_pick_dembrandt_logo(raw),
        color_scheme=None,
        fonts=tuple(_collect_dembrandt_fonts(raw)),
        raw=raw,
    )


def measured_from_stored(stored: object) -> MeasuredBranding:
    """Re-hydrate from our persisted normalized dict (or legacy raw)."""
    if not isinstance(stored, dict) or not stored:
        return MeasuredBranding()
    # Already normalized shape
    if "colors" in stored and isinstance(stored.get("colors"), list):
        colors = tuple(c for c in (_as_hex(v) for v in stored["colors"]) if c)
        logo = stored.get("logo_url")
        logo_url = scrub_free_text(str(logo)) if isinstance(logo, str) and logo.strip() else None
        scheme = stored.get("color_scheme")
        fonts_raw = stored.get("fonts")
        fonts = tuple(
            scrub_free_text(str(f))
            for f in (fonts_raw if isinstance(fonts_raw, list) else [])
            if isinstance(f, str) and f.strip()
        )
        return MeasuredBranding(
            colors=colors,
            logo_url=logo_url,
            color_scheme=scrub_free_text(str(scheme)) if scheme else None,
            fonts=fonts,
            raw=stored,
        )
    return normalize_branding(stored)


def apply_measured_to_draft_fields(
    *,
    measured: MeasuredBranding,
    display_name: str,
    tagline: str,
    color_palette: tuple[str, ...],
    logo_url: str | None,
    site_title: str = "",
    site_description: str = "",
) -> dict[str, object]:
    """Prefer measured visuals; fill blank name/tagline from site metadata."""
    palette = list(measured.colors) if measured.colors else list(color_palette)
    name = display_name.strip() or site_title.strip()
    line = tagline.strip() or site_description.strip()
    logo = logo_url or measured.logo_url
    return {
        "display_name": name,
        "tagline": line,
        "color_palette": palette,
        "logo_url": logo,
        "colors_measured": bool(measured.colors),
    }
