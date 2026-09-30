"""SSRF-safe HTTP fetch — thin wrapper over `ssrf.safe_connect`.

Stock import and any caller that only needs raw bytes uses this entry point.
Website research uses `fetcher.fetch_site` / `ssrf.safe_connect` directly.
"""

from __future__ import annotations

from app.integrations.webfetch.ssrf import safe_connect

_MAX_BYTES = 20 * 1024 * 1024


async def safe_fetch(url: str, *, max_bytes: int = _MAX_BYTES) -> bytes:
    """Fetch `url` with DNS re-resolution and private-IP blocking on every hop."""
    response = await safe_connect(url, max_bytes=max_bytes)
    return response.content
