"""Read-only live Zernio analytics — skipped unless RUN_LIVE_TESTS=1."""

from __future__ import annotations

import os

import pytest

from app.integrations.errors import AnalyticsCursorExpired
from app.integrations.social.zernio import ZernioClient

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="Set RUN_LIVE_TESTS=1 to hit real Zernio accounts",
    ),
]

_ALIAS_ENV_CANDIDATES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("t1", ("ZERNIO_API_KEY__t1", "ZERNIO_API_KEY_1")),
    ("t2", ("ZERNIO_API_KEY__t2", "ZERNIO_API_KEY_2")),
    ("t3", ("ZERNIO_API_KEY__t3", "ZERNIO_API_KEY_3")),
)


def _first_credential() -> tuple[str, str]:
    for alias, env_names in _ALIAS_ENV_CANDIDATES:
        for name in env_names:
            value = os.environ.get(name, "").strip()
            if value:
                return alias, value
    pytest.skip(
        "No Zernio API keys configured "
        "(ZERNIO_API_KEY__t1/t2/t3 or ZERNIO_API_KEY_1/2/3)"
    )


@pytest.mark.asyncio
async def test_analytics_bootstrap_delta() -> None:
    alias, api_key = _first_credential()
    client = ZernioClient(alias=alias, api_key=api_key, analytics_min_interval=0.0)

    baseline = await client.get_analytics_baseline(limit=10)
    assert isinstance(baseline.has_analytics_access, bool)

    delta = await client.get_analytics_delta(limit=10)
    assert isinstance(delta.next_cursor, str)
    assert delta.next_cursor
    assert isinstance(delta.data, list)
    assert isinstance(delta.has_more, bool)


@pytest.mark.asyncio
async def test_analytics_malformed_cursor_expired() -> None:
    alias, api_key = _first_credential()
    client = ZernioClient(alias=alias, api_key=api_key, analytics_min_interval=0.0)

    with pytest.raises(AnalyticsCursorExpired):
        await client.get_analytics_delta(cursor="not-a-real-cursor")
