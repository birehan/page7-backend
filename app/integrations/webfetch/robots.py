"""Per-host robots.txt fetch and check (architecture/08 §2.1).

Fetched exclusively through `safe_connect` — never via
`RobotFileParser.read()`, which uses urllib and would bypass SSRF validation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

from app.integrations.errors import ProviderTimeoutError, ProviderUnavailableError
from app.integrations.webfetch.ssrf import safe_connect

_USER_AGENT = "page7-research"
_CACHE_TTL_SECONDS = 3600.0


@dataclass
class _CacheEntry:
    parser: RobotFileParser
    fetched_at: float
    fetch_failed: bool


_cache: dict[str, _CacheEntry] = {}


def clear_robots_cache() -> None:
    """Test helper — drop the in-process robots cache."""
    _cache.clear()


def _robots_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    return f"{parsed.scheme}://{parsed.netloc}/robots.txt"


async def _load_parser(source_url: str) -> _CacheEntry:
    host_key = urlparse(source_url).netloc.lower()
    now = time.monotonic()
    cached = _cache.get(host_key)
    if cached is not None and (now - cached.fetched_at) < _CACHE_TTL_SECONDS:
        return cached

    parser = RobotFileParser()
    robots_url = _robots_url(source_url)
    fetch_failed = False
    try:
        response = await safe_connect(robots_url, timeout_seconds=10.0, max_bytes=512 * 1024)
        text = response.content.decode("utf-8", errors="replace")
        parser.parse(text.splitlines())
    except (ProviderUnavailableError, ProviderTimeoutError, UnicodeError):
        # Fail open on unreachable robots.txt (common for small sites), but
        # still cache the miss so we don't re-hit on every page.
        parser.parse([])
        fetch_failed = True

    entry = _CacheEntry(parser=parser, fetched_at=now, fetch_failed=fetch_failed)
    _cache[host_key] = entry
    return entry


async def is_allowed(url: str) -> bool:
    """Return False when robots.txt disallows `url` for our user-agent."""
    entry = await _load_parser(url)
    if entry.fetch_failed:
        return True
    return bool(entry.parser.can_fetch(_USER_AGENT, url))
