from __future__ import annotations

import base64
import json
from typing import Any

from app.core.schema import CamelModel


class Page[T](CamelModel):
    """The generic cursor-paginated envelope (ADR-0008), mirroring the
    frontend's own `paginated()` helper (`shared/api/contracts/primitives.ts`)
    field for field.
    """

    items: list[T]
    next_cursor: str | None = None


class InvalidCursorError(ValueError):
    def __init__(self, cursor: str) -> None:
        super().__init__(f"invalid pagination cursor: {cursor!r}")


def encode_cursor(payload: dict[str, Any]) -> str:
    """Opaque cursor pagination (architecture/04 §Pagination, ADR-0008): callers
    must treat the returned string as opaque and never construct one by hand. The
    payload is whatever the caller's sort key needs — typically
    {"created_at": ..., "id": ...} to break ties on the fixed `created_at DESC, id
    DESC` ordering every paginated endpoint uses.
    """
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        decoded = json.loads(raw)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError(cursor) from exc
    if not isinstance(decoded, dict):
        raise InvalidCursorError(cursor)
    return decoded
