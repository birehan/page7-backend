"""Live spike: does Qwen Image 3 render Arabic text?

Skipped unless RUN_LIVE_TESTS=1. Budget-capped (~4 images). Writes output URLs
to a scratch dir for human review of glyph shaping, letter joining, diacritics,
and RTL word order. This is a blocking gate for the poster style (plan step 4).

Run:
  RUN_LIVE_TESTS=1 uv run pytest -m live -k qwen_arabic -s
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import fal_client
import pytest

from app.core.config import get_settings

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="Set RUN_LIVE_TESTS=1 to hit real Fal accounts",
    ),
]

_SCRATCH = Path(tempfile.gettempdir()) / "pgblank-qwen-arabic-spike"

_ARABIC_PROMPTS: list[tuple[str, str]] = [
    (
        "gulf_offer",
        "A clean social media poster for a Riyadh coffee shop. Large Arabic "
        "headline reading 'عرض خاص هذا الأسبوع' centered, modern Saudi aesthetic, "
        "warm lighting, no English text.",
    ),
    (
        "bilingual_greeting",
        "A bilingual greeting card graphic. Top line Arabic 'رمضان كريم' and "
        "below it English 'Ramadan Kareem', elegant typography, soft gold and "
        "cream palette, square format.",
    ),
    (
        "menu_price",
        "A restaurant menu item card. Arabic dish name 'كبسة دجاج' with price "
        "'٤٥ ر.س' clearly readable, appetizing food photography background, "
        "minimal white overlay for the text.",
    ),
]


def _api_key() -> str:
    settings = get_settings()
    if settings.imagegen.api_key is not None:
        return settings.imagegen.api_key.get_secret_value()
    fal_key = os.environ.get("FAL_KEY", "").strip()
    if fal_key:
        return fal_key
    pytest.skip("IMAGEGEN__API_KEY or FAL_KEY required for live fal test")


@pytest.mark.asyncio
async def test_live_qwen_arabic_poster_spike() -> None:
    """Generate 3 Arabic Qwen posters + 1 flux/dev control; dump URLs for review."""
    api_key = _api_key()
    os.environ.setdefault("FAL_KEY", api_key)
    _SCRATCH.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240

    results: dict[str, object] = {}

    for name, prompt in _ARABIC_PROMPTS:
        raw = await fal_client.subscribe_async(
            "alibaba/qwen-image-3/text-to-image",
            arguments={
                "prompt": prompt,
                "image_size": "square_hd",
                "num_images": 1,
                "output_format": "png",
                "enable_prompt_expansion": True,
            },
            client_timeout=180.0,
        )
        assert isinstance(raw, dict), raw
        images = raw.get("images")
        assert isinstance(images, list) and images, raw
        url = images[0].get("url") if isinstance(images[0], dict) else None
        assert isinstance(url, str) and url.startswith("https://"), raw
        results[f"qwen_{name}"] = {
            "url": url,
            "width": images[0].get("width"),
            "height": images[0].get("height"),
            "prompt": prompt,
        }

    # Flux control — expected to garble Arabic; documents the baseline gap.
    flux_raw = await fal_client.subscribe_async(
        "fal-ai/flux/dev",
        arguments={
            "prompt": _ARABIC_PROMPTS[0][1],
            "image_size": "square_hd",
            "num_images": 1,
            "enable_safety_checker": True,
            "num_inference_steps": 20,
        },
        client_timeout=180.0,
    )
    assert isinstance(flux_raw, dict)
    flux_images = flux_raw.get("images")
    assert isinstance(flux_images, list) and flux_images
    flux_url = flux_images[0].get("url") if isinstance(flux_images[0], dict) else None
    assert isinstance(flux_url, str) and flux_url.startswith("https://")
    results["flux_control_gulf_offer"] = {
        "url": flux_url,
        "prompt": _ARABIC_PROMPTS[0][1],
        "note": "flux/dev control — Arabic shaping expected to fail",
    }

    out_path = _SCRATCH / "results.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nQwen Arabic spike results written to {out_path}")
    for key, value in results.items():
        assert isinstance(value, dict)
        print(f"  {key}: {value.get('url')}")
