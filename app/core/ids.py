from __future__ import annotations

import os
import time
from uuid import UUID


def new_uuid7() -> UUID:
    """Generate a UUIDv7 (RFC 9562) application-side: a 48-bit big-endian Unix
    millisecond timestamp followed by random bits, version/variant nibbles set per
    spec. Postgres 18's native `uuidv7()` default (ADR-0009) mints the id for
    almost every insert; this exists for the rare case application code needs the
    id before the row is written (e.g. to reference it elsewhere in the same
    request/transaction).
    """
    unix_ms = time.time_ns() // 1_000_000
    rand = os.urandom(10)
    buf = bytearray(16)
    buf[0:6] = unix_ms.to_bytes(6, "big")
    buf[6] = 0x70 | (rand[0] & 0x0F)  # version 7
    buf[7] = rand[1]
    buf[8] = 0x80 | (rand[2] & 0x3F)  # variant 10
    buf[9:16] = rand[3:10]
    return UUID(bytes=bytes(buf))


def is_valid_uuid(value: str) -> bool:
    """Validates a client-supplied id is a well-formed UUID before it ever reaches
    a query — every client-mintable id (idempotency keys aside) is checked this way
    at the API boundary rather than relying on the database to reject it.
    """
    try:
        UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True
