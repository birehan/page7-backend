"""Parse Zernio analytics delta/baseline payloads into port models."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.errors import (
    AnalyticsCursorExpired,
    ProviderPaymentRequiredError,
    ProviderUnavailableError,
)
from app.integrations.social.fakes import FakeSocialProvider
from app.integrations.social.zernio import (
    ZernioClient,
    _map_analytics_baseline,
    _map_analytics_delta,
)


def test_map_analytics_delta_camel_case() -> None:
    result = _map_analytics_delta(
        {
            "data": [
                {
                    "postId": "zp1",
                    "accountId": "acc1",
                    "profileId": "prof1",
                    "platform": "instagram",
                    "platformPostId": "ext1",
                    "publishedAt": "2024-11-01T10:00:05Z",
                    "syncedAt": "2024-11-02T08:30:00Z",
                    "isDeleted": False,
                    "metrics": {"impressions": 10, "likes": 2},
                }
            ],
            "nextCursor": "cur_abc",
            "hasMore": True,
        }
    )
    assert result.next_cursor == "cur_abc"
    assert result.has_more is True
    assert len(result.data) == 1
    entry = result.data[0]
    assert entry.post_id == "zp1"
    assert entry.account_id == "acc1"
    assert entry.profile_id == "prof1"
    assert entry.platform == "instagram"
    assert entry.platform_post_id == "ext1"
    assert entry.metrics["impressions"] == 10
    assert entry.is_deleted is False


def test_map_analytics_delta_empty_bootstrap() -> None:
    result = _map_analytics_delta(
        {"data": [], "nextCursor": "boot", "hasMore": False}
    )
    assert result.data == []
    assert result.next_cursor == "boot"
    assert result.has_more is False


def test_map_analytics_baseline() -> None:
    result = _map_analytics_baseline(
        {
            "hasAnalyticsAccess": True,
            "accounts": [
                {
                    "accountId": "acc1",
                    "platform": "instagram",
                    "followerCount": 99,
                }
            ],
            "posts": [
                {
                    "postId": "zp1",
                    "accountId": "acc1",
                    "platform": "instagram",
                    "publishedAt": "2024-11-01T10:00:05Z",
                    "analytics": {"impressions": 5},
                }
            ],
        }
    )
    assert result.has_analytics_access is True
    assert result.accounts[0].follower_count == 99
    assert result.posts[0].metrics["impressions"] == 5


def _response(
    status: int,
    *,
    json_body: dict[str, Any] | list[Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=json_body if json_body is not None else {},
        headers=headers or {},
        request=httpx.Request("GET", "https://zernio.com/api/v1/analytics/delta"),
    )


@pytest.mark.asyncio
async def test_client_delta_400_raises_cursor_expired() -> None:
    client = ZernioClient(
        alias="t-delta", api_key="sk_test", analytics_min_interval=0.0
    )
    mock_response = _response(
        400, json_body={"error": "Cursor expired", "code": "CURSOR_EXPIRED"}
    )
    mock_http = MagicMock()
    mock_http.request = AsyncMock(return_value=mock_response)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.integrations.social.zernio.httpx.AsyncClient", return_value=mock_http),
        pytest.raises(AnalyticsCursorExpired),
    ):
        await client.get_analytics_delta(cursor="stale")


@pytest.mark.asyncio
async def test_client_delta_402_raises_payment_required() -> None:
    client = ZernioClient(
        alias="t-delta-402", api_key="sk_test", analytics_min_interval=0.0
    )
    mock_response = _response(
        402,
        json_body={"error": "addon", "code": "analytics_addon_required"},
    )
    mock_http = MagicMock()
    mock_http.request = AsyncMock(return_value=mock_response)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.integrations.social.zernio.httpx.AsyncClient", return_value=mock_http),
        pytest.raises(ProviderPaymentRequiredError),
    ):
        await client.get_analytics_delta()


@pytest.mark.asyncio
async def test_client_delta_503_includes_retry_after() -> None:
    client = ZernioClient(
        alias="t-delta-503", api_key="sk_test", analytics_min_interval=0.0
    )
    mock_response = _response(
        503,
        json_body={"error": "busy", "type": "api_error"},
        headers={"Retry-After": "7"},
    )
    mock_http = MagicMock()
    mock_http.request = AsyncMock(return_value=mock_response)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.integrations.social.zernio.httpx.AsyncClient", return_value=mock_http),
        pytest.raises(ProviderUnavailableError) as exc_info,
    ):
        await client.get_analytics_delta()
    assert exc_info.value.retry_after == 7.0


@pytest.mark.asyncio
async def test_fake_delta_scripts() -> None:
    provider = FakeSocialProvider()
    provider.analytics_delta_script = "non_empty"
    non_empty = await provider.get_analytics_delta()
    assert len(non_empty.data) == 1
    assert non_empty.next_cursor

    provider.analytics_delta_script = "empty_page"
    empty = await provider.get_analytics_delta()
    assert empty.data == []

    provider.analytics_delta_script = "has_more_chain"
    first = await provider.get_analytics_delta()
    assert first.has_more is True
    second = await provider.get_analytics_delta()
    assert second.has_more is False

    provider.analytics_delta_script = "addon_required"
    with pytest.raises(ProviderPaymentRequiredError):
        await provider.get_analytics_delta()

    provider.analytics_delta_script = "unavailable_503"
    with pytest.raises(ProviderUnavailableError) as exc_info:
        await provider.get_analytics_delta()
    assert exc_info.value.retry_after == 5.0

    provider.analytics_delta_script = "cursor_expired_400"
    with pytest.raises(AnalyticsCursorExpired):
        await provider.get_analytics_delta(cursor="old")
