from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class StockAttribution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    author: str | None = None
    author_url: str | None = None
    source_url: str | None = None
    license: str | None = None


class StockPhoto(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    thumb_url: str
    full_url: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    description: str
    attribution: StockAttribution


class StockSearchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[StockPhoto]
    next_page: int | None = None


@runtime_checkable
class StockProvider(Protocol):
    async def search(self, *, query: str, page: int) -> StockSearchResult: ...

    async def fetch_full(self, stock_id: str) -> bytes | str: ...
