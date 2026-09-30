"""Unit tests for Zernio errorCategory → our lastError mapping."""

from __future__ import annotations

import pytest

from app.features.publishing.service import map_error_category


@pytest.mark.parametrize(
    ("zernio", "code", "retryable", "max_attempts", "mark_expired"),
    [
        ("user_content", "PLATFORM_REJECTED", False, 0, False),
        ("platform_rejected", "PLATFORM_REJECTED", False, 0, False),
        ("user_abuse", "PLATFORM_REJECTED", False, 0, False),
        ("auth_expired", "ACCOUNT_TOKEN_EXPIRED", False, 0, True),
        ("account_issue", "ACCOUNT_ISSUE", False, 0, False),
        ("platform_error", "PLATFORM_ERROR", True, 3, False),
        ("system_error", "PROVIDER_ERROR", True, 3, False),
        ("platform_rate_limit", "PLATFORM_RATE_LIMIT", True, 3, False),
        ("quota_exhausted", "QUOTA_EXHAUSTED", False, 0, False),
        ("unknown", "PROVIDER_ERROR", True, 2, False),
        (None, "PROVIDER_ERROR", True, 2, False),
        ("not_a_real_category", "PROVIDER_ERROR", True, 2, False),
    ],
)
def test_map_error_category(
    zernio: str | None,
    code: str,
    retryable: bool,
    max_attempts: int,
    mark_expired: bool,
) -> None:
    mapped = map_error_category(zernio)
    assert mapped.code == code
    assert mapped.retryable is retryable
    assert mapped.max_attempts == max_attempts
    assert mapped.mark_account_expired is mark_expired
    assert mapped.default_message


def test_publication_categories_are_ledger_enums() -> None:
    allowed = {
        "auth",
        "rate_limit",
        "validation",
        "platform",
        "network",
        "frozen",
        "internal",
    }
    for key in (
        "user_content",
        "auth_expired",
        "platform_error",
        "system_error",
        "platform_rate_limit",
        "quota_exhausted",
        "unknown",
    ):
        assert map_error_category(key).publication_category in allowed
