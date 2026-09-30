"""Self-hosted Firecrawl scraper over httpx (vendored for brand_approaches lab)."""

from __future__ import annotations

import logging
import os

import httpx

from .branding import normalize_branding
from .scrub import scrub_free_text, scrub_free_text_mapping
from .types import AiUnavailableError, ConfigurationError, CrawlResult

log = logging.getLogger(__name__)

PROVIDER = "firecrawl"
_RETRYABLE_STATUSES = frozenset({408, 409, 429})
_TIMEOUT_SECONDS = 120.0
_DEFAULT_BASE = "http://127.0.0.1:3002"


class FirecrawlCrawler:
    """Renders one page and returns scrubbed markdown."""

    def __init__(
        self, *, base_url: str | None = None, client: httpx.AsyncClient | None = None
    ) -> None:
        resolved = (base_url or os.environ.get("FIRECRAWL_BASE_URL") or _DEFAULT_BASE).rstrip("/")
        self._base_url = resolved
        self._client = client or httpx.AsyncClient(base_url=resolved, timeout=_TIMEOUT_SECONDS)

    @property
    def base_url(self) -> str:
        return self._base_url

    async def crawl(self, url: str) -> CrawlResult:
        response = await self._post_scrape(url, formats=["markdown"])
        return self._to_result(url, response)

    async def _post_scrape(self, url: str, *, formats: list[str]) -> httpx.Response:
        try:
            response = await self._client.post(
                "/v1/scrape",
                json={"url": url, "formats": formats, "onlyMainContent": True},
            )
        except httpx.HTTPError as exc:
            raise AiUnavailableError(f"Could not reach Firecrawl: {type(exc).__name__}") from exc
        return response

    def _to_result(self, url: str, response: httpx.Response) -> CrawlResult:
        if response.status_code in _RETRYABLE_STATUSES or response.status_code >= 500:
            raise AiUnavailableError(f"Firecrawl returned {response.status_code}")
        if response.status_code >= 400:
            raise ConfigurationError(
                f"Firecrawl rejected the request with {response.status_code}; "
                "the URL or the request shape is wrong, not the connection."
            )

        body = response.json()
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise AiUnavailableError("Firecrawl returned no data object")

        metadata = data.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        measured = normalize_branding(data.get("branding"))

        log.info(
            "firecrawl crawl completed url=%s markdown_len=%s",
            url,
            len(str(data.get("markdown", ""))),
        )

        return CrawlResult(
            url=url,
            title=scrub_free_text(str(metadata.get("title", ""))),
            markdown=scrub_free_text(str(data.get("markdown", ""))),
            metadata=scrub_free_text_mapping(metadata),
            branding=measured.as_dict(),
        )
