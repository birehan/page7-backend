from __future__ import annotations

from typing import Annotated

from fastapi import Depends

from app.core.config import Environment, Settings, get_settings
from app.integrations.stock.fakes import FakeStockProvider
from app.integrations.stock.ports import StockProvider
from app.integrations.stock.unsplash import UnsplashStockProvider

_LOCAL_ENVS = (Environment.DEVELOPMENT, Environment.TESTING)


def get_stock_provider(settings: Annotated[Settings, Depends(get_settings)]) -> StockProvider:
    resolve_to_fake = settings.stock.provider == "fake" or (
        settings.stock.provider == "auto" and settings.app_env in _LOCAL_ENVS
    )
    if resolve_to_fake:
        return FakeStockProvider()
    key = settings.stock.unsplash_access_key
    if key is None:
        raise RuntimeError("STOCK__UNSPLASH_ACCESS_KEY is not configured")
    return UnsplashStockProvider(access_key=key.get_secret_value())
