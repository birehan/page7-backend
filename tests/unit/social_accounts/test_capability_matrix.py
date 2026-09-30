"""Capability matrix matches architecture/10 §4."""

from __future__ import annotations

from app.features.social_accounts.service import capability_matrix


def test_capability_matrix_exact() -> None:
    matrix = capability_matrix()
    by_platform = {row.platform: row for row in matrix.platforms}

    assert set(by_platform) == {
        "instagram",
        "facebook",
        "tiktok",
        "snapchat",
        "whatsapp",
    }
    assert by_platform["instagram"].connectable is True
    assert by_platform["instagram"].publishable is True
    assert by_platform["facebook"].connectable is True
    assert by_platform["facebook"].publishable is True
    assert by_platform["tiktok"].connectable is True
    assert by_platform["tiktok"].publishable is False
    assert by_platform["snapchat"].connectable is False
    assert by_platform["snapchat"].publishable is False
    assert by_platform["whatsapp"].connectable is False
    assert by_platform["whatsapp"].publishable is False
