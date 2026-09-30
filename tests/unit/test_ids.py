from __future__ import annotations

import uuid

from app.core.ids import is_valid_uuid, new_uuid7


def test_new_uuid7_has_correct_version_and_variant() -> None:
    value = new_uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_new_uuid7_is_monotonically_increasing_across_calls() -> None:
    first, second = new_uuid7(), new_uuid7()
    assert first.bytes[:6] <= second.bytes[:6]


def test_is_valid_uuid() -> None:
    assert is_valid_uuid(str(new_uuid7())) is True
    assert is_valid_uuid("not-a-uuid") is False
    assert is_valid_uuid("") is False
