"""Fake imagegen bytes must be a browser-decodable PNG (not signature-only)."""

from __future__ import annotations

import struct
import zlib

from app.integrations.imagegen.fakes import FAKE_PNG_BYTES


def test_fake_png_bytes_are_valid_png() -> None:
    assert FAKE_PNG_BYTES.startswith(b"\x89PNG\r\n\x1a\n")
    assert FAKE_PNG_BYTES.endswith(b"IEND\xaeB`\x82")
    # IHDR width/height are 1×1 — enough for <img> to decode and fill via CSS.
    ihdr = FAKE_PNG_BYTES[8 : 8 + 8 + 13 + 4]
    length, chunk_type = struct.unpack(">I4s", ihdr[:8])
    assert length == 13
    assert chunk_type == b"IHDR"
    width, height = struct.unpack(">II", ihdr[8:16])
    assert (width, height) == (1, 1)
    # Round-trip inflate of IDAT so we know the payload isn't truncated junk.
    idat_start = FAKE_PNG_BYTES.index(b"IDAT") + 4
    idat_len = struct.unpack(">I", FAKE_PNG_BYTES[idat_start - 8 : idat_start - 4])[0]
    zlib.decompress(FAKE_PNG_BYTES[idat_start : idat_start + idat_len])
