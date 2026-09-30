from __future__ import annotations

import pytest

from app.core.pagination import InvalidCursorError, decode_cursor, encode_cursor


def test_round_trips_a_payload() -> None:
    payload = {"created_at": "2026-09-04T00:00:00Z", "id": "abc123"}
    assert decode_cursor(encode_cursor(payload)) == payload


def test_cursor_is_opaque_ascii() -> None:
    assert encode_cursor({"id": "x"}).isascii()


def test_rejects_garbage_input() -> None:
    with pytest.raises(InvalidCursorError):
        decode_cursor("not-a-real-cursor!!!")


def test_rejects_a_non_object_payload() -> None:
    import base64
    import json

    cursor = base64.urlsafe_b64encode(json.dumps([1, 2, 3]).encode()).decode()
    with pytest.raises(InvalidCursorError):
        decode_cursor(cursor)
