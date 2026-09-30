from __future__ import annotations

from app.integrations.stock.ports import StockAttribution, StockPhoto, StockSearchResult


class FakeStockProvider:
    """Deterministic stock search for unit/integration tests."""

    def __init__(self) -> None:
        self.imported: list[str] = []

    async def search(self, *, query: str, page: int) -> StockSearchResult:
        if page > 2:
            return StockSearchResult(items=[], next_page=None)
        items = [
            StockPhoto(
                id=f"stock_{query or 'empty'}-{page}-{i}",
                thumb_url=f"https://images.test/{page}-{i}-thumb.jpg",
                full_url=f"https://images.test/{page}-{i}.jpg",
                width=1200,
                height=800,
                description=f"{query} photo {i}",
                attribution=StockAttribution(
                    provider="unsplash",
                    author=f"Photographer {i}",
                    author_url=f"https://unsplash.com/@p{i}",
                    source_url=f"https://unsplash.com/photos/{page}-{i}",
                    license="Unsplash License",
                ),
            )
            for i in range(3)
        ]
        return StockSearchResult(items=items, next_page=page + 1 if page < 2 else None)

    async def fetch_full(self, stock_id: str) -> bytes | str:
        self.imported.append(stock_id)
        # 1x1 PNG
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00"
            b"\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )
