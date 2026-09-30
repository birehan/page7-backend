"""Unit tests for content_ai/service.py's LLM-output sanitization helpers."""

from __future__ import annotations

from app.features.content_ai.service import _normalize_hashtags


def test_normalize_hashtags_fixes_url_encoded_hash_prefix() -> None:
    # Regression: a real live run against gpt-4o-mini via OpenRouter produced
    # ["#دعم_العملاء", "%23Stripe", "%23خدمة_متميزة"] for hashtags_ar — a raw
    # provider quirk (literal "%23" instead of "#") that, left unsanitized,
    # doubled up as "#%23Stripe" once the frontend's own `#`-prefix guard ran
    # on top of it. This must be fixed at the point the LLM output is turned
    # into a PlanDraftItemIn, before it's ever persisted to posts.variants.
    assert _normalize_hashtags(["#دعم_العملاء", "%23Stripe", "%23خدمة_متميزة"]) == [
        "#دعم_العملاء",
        "#Stripe",
        "#خدمة_متميزة",
    ]


def test_normalize_hashtags_fixes_literal_hash_percent_prefix() -> None:
    # Regression: a second, distinct live shape from the same model —
    # ["#Stripe", "#%الأمان", "#%الابتكار"] — a literal "#%" instead of "#".
    assert _normalize_hashtags(["#Stripe", "#%الأمان", "#%الابتكار"]) == [
        "#Stripe",
        "#الأمان",
        "#الابتكار",
    ]


def test_normalize_hashtags_adds_missing_hash_prefix() -> None:
    assert _normalize_hashtags(["Stripe", "#Already"]) == ["#Stripe", "#Already"]


def test_normalize_hashtags_drops_blank_entries() -> None:
    assert _normalize_hashtags(["#a", "  ", "", "#b", "%23", "#%"]) == ["#a", "#b"]


def test_normalize_hashtags_empty_list_stays_empty() -> None:
    assert _normalize_hashtags([]) == []
