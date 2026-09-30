"""Bounded same-site fetch + extraction orchestrator (architecture/08 §2.1)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from urllib.parse import urlparse

from app.integrations.errors import ProviderTimeoutError, ProviderUnavailableError
from app.integrations.webfetch.extract import PageExtraction, extract_page
from app.integrations.webfetch.robots import is_allowed
from app.integrations.webfetch.ssrf import safe_connect

_PAGE_MAX_BYTES = 5 * 1024 * 1024
_PAGE_TIMEOUT_SECONDS = 15.0
_DEFAULT_PAGE_CAP = 5
_FALLBACK_PATHS = ("/about", "/about-us", "/company", "/contact", "/en/about", "/sa-en")


@dataclass
class FetchOutcome:
    """Result of fetching one URL — success, robots disallow, or error."""

    url: str
    extraction: PageExtraction | None = None
    robots_disallowed: bool = False
    error: str | None = None
    timed_out: bool = False


@dataclass
class SiteFetchResult:
    source_url: str
    pages: list[PageExtraction] = field(default_factory=list)
    outcomes: list[FetchOutcome] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    content_hash: str | None = None

    @property
    def crawled_pages(self) -> int:
        return len(self.pages)


def content_hash_for_pages(pages: list[PageExtraction]) -> str:
    """sha256 over concatenated normalized text, sorted by URL for stability."""
    parts = [f"{page.url}\n{page.text.strip()}" for page in sorted(pages, key=lambda p: p.url)]
    payload = "\n---\n".join(parts).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


async def _fetch_one(url: str) -> FetchOutcome:
    if not await is_allowed(url):
        return FetchOutcome(url=url, robots_disallowed=True)
    try:
        response = await safe_connect(
            url,
            max_bytes=_PAGE_MAX_BYTES,
            timeout_seconds=_PAGE_TIMEOUT_SECONDS,
        )
    except ProviderTimeoutError as exc:
        return FetchOutcome(url=url, timed_out=True, error=str(exc))
    except ProviderUnavailableError as exc:
        return FetchOutcome(url=url, error=str(exc))

    content_type = response.content_type.lower()
    if "text/html" not in content_type and "application/xhtml" not in content_type:
        return FetchOutcome(
            url=url,
            error=f"unsupported content-type: {response.content_type or 'missing'}",
        )
    extraction = extract_page(response.content, page_url=response.url)
    return FetchOutcome(url=url, extraction=extraction)


async def fetch_site(
    source_url: str,
    *,
    page_cap: int = _DEFAULT_PAGE_CAP,
) -> SiteFetchResult:
    """Fetch the brand homepage plus a few same-site about/contact/pricing pages."""
    parsed = urlparse(source_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ProviderUnavailableError("only http(s) URLs are allowed")

    result = SiteFetchResult(source_url=source_url)
    homepage = await _fetch_one(source_url)
    result.outcomes.append(homepage)

    if homepage.robots_disallowed:
        result.warnings.append(f"robots.txt disallows {source_url}")
        return result
    if homepage.timed_out:
        result.warnings.append(f"{source_url} timed out")
        return result
    if homepage.error or homepage.extraction is None:
        result.warnings.append(homepage.error or f"could not fetch {source_url}")
        # Homepage often is a huge SPA; still try known about/contact paths.
        base = f"{parsed.scheme}://{parsed.netloc}"
        for path in _FALLBACK_PATHS:
            if len(result.pages) >= page_cap:
                break
            outcome = await _fetch_one(f"{base}{path}")
            result.outcomes.append(outcome)
            if outcome.extraction is not None:
                result.pages.append(outcome.extraction)
            elif outcome.error:
                result.warnings.append(outcome.error)
        if result.pages:
            result.content_hash = content_hash_for_pages(result.pages)
        return result

    result.pages.append(homepage.extraction)
    candidates = [
        link
        for link in homepage.extraction.same_site_links
        if link.rstrip("/") != source_url.rstrip("/")
    ]

    for link in candidates:
        if len(result.pages) >= page_cap:
            break
        outcome = await _fetch_one(link)
        result.outcomes.append(outcome)
        if outcome.robots_disallowed:
            result.warnings.append(f"robots.txt disallows {link}")
            continue
        if outcome.timed_out:
            result.warnings.append(f"{link} timed out")
            continue
        if outcome.error or outcome.extraction is None:
            result.warnings.append(outcome.error or f"could not fetch {link}")
            continue
        result.pages.append(outcome.extraction)

    if result.pages:
        result.content_hash = content_hash_for_pages(result.pages)
    return result
