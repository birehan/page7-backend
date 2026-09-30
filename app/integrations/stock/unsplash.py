from __future__ import annotations

from typing import Any

import httpx

from app.integrations.errors import (
    ProviderRateLimitedError,
    ProviderUnavailableError,
)
from app.integrations.stock.ports import StockAttribution, StockPhoto, StockSearchResult

_UNSPLASH_SEARCH = "https://api.unsplash.com/search/photos"
_PER_PAGE = 12


class UnsplashStockProvider:
    """The only file allowed to talk to Unsplash's HTTP API."""

    def __init__(self, *, access_key: str) -> None:
        self._access_key = access_key

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Client-ID {self._access_key}"}

    async def search(self, *, query: str, page: int) -> StockSearchResult:
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(
                    _UNSPLASH_SEARCH,
                    params={"query": query, "page": page, "per_page": _PER_PAGE},
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(str(exc)) from exc
        if response.status_code == 429:
            raise ProviderRateLimitedError()
        if response.status_code >= 400:
            raise ProviderUnavailableError(
                f"unsplash search failed: {response.status_code}"
            )
        payload: dict[str, Any] = response.json()
        results = payload.get("results") or []
        total_pages = int(payload.get("total_pages") or 0)
        items = [_map_photo(row) for row in results]
        next_page = page + 1 if page < total_pages else None
        return StockSearchResult(items=items, next_page=next_page)

    async def fetch_full(self, stock_id: str) -> bytes | str:
        # Prefer returning the download URL so the caller can stream via
        # the SSRF-safe fetcher rather than buffering here.
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(
                    f"https://api.unsplash.com/photos/{stock_id}",
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(str(exc)) from exc
        if response.status_code >= 400:
            raise ProviderUnavailableError(
                f"unsplash photo lookup failed: {response.status_code}"
            )
        payload = response.json()
        urls = payload.get("urls") or {}
        full = urls.get("full") or urls.get("raw")
        if not isinstance(full, str):
            raise ProviderUnavailableError("unsplash photo has no full URL")
        return full


def _map_photo(row: dict[str, Any]) -> StockPhoto:
    user = row.get("user") or {}
    urls = row.get("urls") or {}
    links = row.get("links") or {}
    return StockPhoto(
        id=str(row["id"]),
        thumb_url=str(urls.get("thumb") or urls.get("small") or ""),
        full_url=str(urls.get("full") or urls.get("regular") or ""),
        width=int(row.get("width") or 1),
        height=int(row.get("height") or 1),
        description=str(row.get("description") or row.get("alt_description") or ""),
        attribution=StockAttribution(
            provider="unsplash",
            author=user.get("name"),
        author_url=(
            user.get("links", {}).get("html")
            if isinstance(user.get("links"), dict)
            else None
        ),
            source_url=links.get("html"),
            license="Unsplash License",
        ),
    )
