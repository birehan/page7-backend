"""Unit tests for per-megapixel image cost (architecture/09 §6)."""

from __future__ import annotations

from decimal import Decimal

from app.integrations.imagegen.cost import estimate_image_cost_usd


def test_flux_dev_square_hd_single_image() -> None:
    # 1024×1024 ≈ 1.048576 MP × $0.025 ≈ $0.0262144
    cost = estimate_image_cost_usd("fal-ai/flux/dev", 1024, 1024)
    assert cost == Decimal("1024") * Decimal("1024") / Decimal("1000000") * Decimal(
        "0.025"
    )


def test_four_image_flux_dev_batch_near_ten_cents() -> None:
    """architecture/09 §6 worked example: ~$0.10 for four square_hd flux/dev."""
    per = estimate_image_cost_usd("fal-ai/flux/dev", 1024, 1024)
    total = per * 4
    assert Decimal("0.09") < total < Decimal("0.11")


def test_flux_2_flash_cheaper_than_dev() -> None:
    dev = estimate_image_cost_usd("fal-ai/flux/dev", 1024, 1024)
    flash = estimate_image_cost_usd("fal-ai/flux-2/flash", 1024, 1024)
    assert flash < dev
    assert flash == Decimal("1024") * Decimal("1024") / Decimal("1000000") * Decimal(
        "0.005"
    )


def test_flux_2_pro_single_megapixel_flat_rate() -> None:
    # 1024x1024 rounds up to 1 MP -> flat $0.03 (first-MP tier, no extra).
    cost = estimate_image_cost_usd("fal-ai/flux-2/pro", 1024, 1024)
    assert cost == Decimal("0.03")


def test_flux_2_pro_tiered_beyond_first_megapixel() -> None:
    # 1920x1080 ~= 2.0736 MP -> rounds to 2 MP -> 0.03 + 0.015 = 0.045
    # (fal's own worked example for this exact resolution).
    cost = estimate_image_cost_usd("fal-ai/flux-2/pro", 1920, 1080)
    assert cost == Decimal("0.045")


def test_unknown_model_costs_zero_with_warning() -> None:
    assert estimate_image_cost_usd("fal-ai/unknown", 1024, 1024) == Decimal("0")


def test_qwen_1k_and_2k_per_image_rates() -> None:
    one_k = estimate_image_cost_usd(
        "alibaba/qwen-image-3/text-to-image", 1024, 1024
    )
    two_k = estimate_image_cost_usd(
        "alibaba/qwen-image-3/text-to-image", 2048, 2048
    )
    assert one_k == Decimal("0.04")
    assert two_k == Decimal("0.075")
    edit = estimate_image_cost_usd("alibaba/qwen-image-3/edit", 1024, 1024)
    assert edit == Decimal("0.04")
