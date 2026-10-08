"""Per-model image cost (architecture/09 §6).

Flux/dev and Flux 2 Flash are priced per megapixel (flat rate). Flux 2 Pro is
tiered: $0.03 for the first megapixel, then $0.015 per additional megapixel,
rounded to the nearest megapixel (fal's own worked example). Qwen Image 3 is
priced per image at 1K/2K tiers.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

import structlog

log = structlog.get_logger(__name__)

# Rates verified in docs/reference/provider-facts.md.
_MODEL_RATES_PER_MP: dict[str, Decimal] = {
    "fal-ai/flux/dev": Decimal("0.025"),
    "fal-ai/flux-2/flash": Decimal("0.005"),
}

# Tiered models: (first_mp_rate, extra_mp_rate). fal charges the first-tier
# rate for the first megapixel then the extra-tier rate per additional MP,
# rounded up to the nearest megapixel.
_MODEL_RATES_TIERED_MP: dict[str, tuple[Decimal, Decimal]] = {
    "fal-ai/flux-2-pro": (Decimal("0.03"), Decimal("0.015")),
    "fal-ai/flux-2/pro": (Decimal("0.03"), Decimal("0.015")),
}

# Qwen Image 3: pay-per-image (1K ≈ ≤1.05 MP, 2K above).
_MODEL_RATES_PER_IMAGE: dict[str, tuple[Decimal, Decimal]] = {
    "alibaba/qwen-image-3/text-to-image": (Decimal("0.04"), Decimal("0.075")),
    "alibaba/qwen-image-3/edit": (Decimal("0.04"), Decimal("0.075")),
}

# Ideogram v3 BALANCED flat rate; GPT Image approx medium per-MP.
_MODEL_RATES_FLAT: dict[str, Decimal] = {
    "fal-ai/ideogram/v3": Decimal("0.06"),
}
_MODEL_RATES_PER_MP["fal-ai/gpt-image-1.5"] = Decimal("0.034")

_MP = Decimal("1000000")
_ONE_K_PIXELS = Decimal("1024") * Decimal("1024")


def estimate_image_cost_usd(model_id: str, width: int, height: int) -> Decimal:
    """Return billed USD for one image, or 0 with a warning if the model is unpriced."""
    flat = _MODEL_RATES_FLAT.get(model_id)
    if flat is not None:
        return flat

    per_image = _MODEL_RATES_PER_IMAGE.get(model_id)
    if per_image is not None:
        rate_1k, rate_2k = per_image
        pixels = Decimal(width) * Decimal(height)
        return rate_1k if pixels <= _ONE_K_PIXELS else rate_2k

    tiered = _MODEL_RATES_TIERED_MP.get(model_id)
    if tiered is not None:
        first_rate, extra_rate = tiered
        megapixels_exact = (Decimal(width) * Decimal(height)) / _MP
        # fal's own worked examples round to the *nearest* megapixel (not up):
        # 1024x1024 (1.048576 MP) bills as 1 MP ($0.03 flat); 1920x1080
        # (2.0736 MP) bills as 2 MP ($0.045); 512x512 (0.25 MP) floors at a
        # 1 MP minimum. `ROUND_HALF_UP` with a 1 MP floor matches all three.
        megapixels = max(
            Decimal("1"),
            megapixels_exact.to_integral_value(rounding=ROUND_HALF_UP),
        )
        if megapixels <= 1:
            return first_rate
        return first_rate + (megapixels - 1) * extra_rate

    rate = _MODEL_RATES_PER_MP.get(model_id)
    if rate is None:
        log.warning(
            "imagegen_unpriced_model",
            model_id=model_id,
            width=width,
            height=height,
        )
        return Decimal("0")
    megapixels = (Decimal(width) * Decimal(height)) / _MP
    return megapixels * rate
