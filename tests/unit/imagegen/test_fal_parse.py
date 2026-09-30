"""Unit tests for Fal result parsing helpers."""

from __future__ import annotations

from app.integrations.imagegen.fal import _extract_flagged


def test_missing_nsfw_signal_is_unflagged() -> None:
    assert _extract_flagged({"images": []}, index=0) is False


def test_nsfw_list_true_at_index() -> None:
    assert _extract_flagged({"has_nsfw_concepts": [False, True]}, index=1) is True
    assert _extract_flagged({"has_nsfw_concepts": [False, True]}, index=0) is False


def test_nsfw_bool() -> None:
    assert _extract_flagged({"has_nsfw_concepts": True}, index=0) is True


def test_qwen_profile_never_flags_from_missing_signal() -> None:
    assert (
        _extract_flagged({"has_nsfw_concepts": [True]}, index=0, profile="qwen")
        is False
    )
