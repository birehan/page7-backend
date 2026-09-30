"""Robots.txt parsing tests — parser fed via mocked safe_connect."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.integrations.webfetch import robots as robots_mod
from app.integrations.webfetch.ssrf import SafeResponse


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    robots_mod.clear_robots_cache()


@pytest.mark.asyncio
async def test_robots_disallow(monkeypatch: pytest.MonkeyPatch) -> None:
    body = b"User-agent: *\nDisallow: /pricing\nAllow: /\n"

    async def fake_connect(url: str, **kwargs: object) -> SafeResponse:
        assert url.endswith("/robots.txt")
        return SafeResponse(
            url=url,
            status_code=200,
            headers={"content-type": "text/plain"},
            content=body,
            content_type="text/plain",
        )

    monkeypatch.setattr(robots_mod, "safe_connect", fake_connect)
    assert await robots_mod.is_allowed("https://example.sa/") is True
    assert await robots_mod.is_allowed("https://example.sa/pricing") is False
    assert await robots_mod.is_allowed("https://example.sa/about") is True


@pytest.mark.asyncio
async def test_robots_fail_open_on_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.errors import ProviderUnavailableError

    monkeypatch.setattr(
        robots_mod,
        "safe_connect",
        AsyncMock(side_effect=ProviderUnavailableError("down")),
    )
    assert await robots_mod.is_allowed("https://down.sa/anything") is True
