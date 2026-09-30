"""ZernioClient maps HTTP status / type+code to Provider* errors."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderNotFoundError,
    ProviderPaymentRequiredError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
)
from app.integrations.social.zernio import ZernioClient, _raise_for_status


def _response(
    status: int,
    *,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=json_body or {},
        headers=headers or {},
        request=httpx.Request("GET", "https://zernio.com/api/v1/auth/verify"),
    )


def test_raise_for_status_401() -> None:
    with pytest.raises(ProviderAuthError):
        _raise_for_status(
            _response(401, json_body={"type": "authentication_error", "code": "bad_key"})
        )


def test_raise_for_status_404() -> None:
    with pytest.raises(ProviderNotFoundError):
        _raise_for_status(_response(404, json_body={"type": "invalid_request_error"}))


def test_raise_for_status_402() -> None:
    with pytest.raises(ProviderPaymentRequiredError) as exc_info:
        _raise_for_status(
            _response(402, json_body={"type": "permission_error", "code": "PAYMENT_REQUIRED"})
        )
    assert exc_info.value.reason is None
    assert exc_info.value.is_capacity is False


def test_raise_for_status_402_free_tier_exceeded() -> None:
    with pytest.raises(ProviderPaymentRequiredError) as exc_info:
        _raise_for_status(
            _response(
                402,
                json_body={
                    "type": "permission_error",
                    "code": "PAYMENT_REQUIRED",
                    "details": {"reason": "free_tier_exceeded"},
                },
            )
        )
    assert exc_info.value.reason == "free_tier_exceeded"
    assert exc_info.value.is_capacity is True


def test_raise_for_status_429_parses_retry_after() -> None:
    with pytest.raises(ProviderRateLimitedError) as exc_info:
        _raise_for_status(
            _response(
                429,
                json_body={"type": "rate_limit_error", "code": "RATE_LIMIT"},
                headers={"Retry-After": "12.5"},
            )
        )
    assert exc_info.value.retry_after == 12.5


def test_raise_for_status_5xx() -> None:
    with pytest.raises(ProviderUnavailableError):
        _raise_for_status(
            _response(503, json_body={"type": "api_error", "code": "INTERNAL"})
        )


def test_raise_for_status_4xx_generic() -> None:
    with pytest.raises(ProviderError):
        _raise_for_status(
            _response(
                422,
                json_body={"type": "invalid_request_error", "code": "INVALID"},
            )
        )


def test_raise_for_status_branches_on_type_not_message() -> None:
    """Message text must not drive the branch — type/code only."""
    with pytest.raises(ProviderAuthError):
        _raise_for_status(
            _response(
                401,
                json_body={
                    "error": "totally unrelated display text that says rate limit",
                    "type": "authentication_error",
                    "code": "INVALID_KEY",
                },
            )
        )


@pytest.mark.asyncio
async def test_client_verify_auth_success() -> None:
    client = ZernioClient(alias="t1", api_key="sk_test")
    mock_response = _response(
        200,
        json_body={
            "valid": True,
            "userId": "u1",
            "authType": "api_key",
            "scope": "full",
        },
        headers={
            "X-RateLimit-Limit": "600",
            "X-RateLimit-Remaining": "599",
            "X-RateLimit-Reset": "1700000000",
        },
    )
    mock_http = MagicMock()
    mock_http.request = AsyncMock(return_value=mock_response)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)

    with patch("app.integrations.social.zernio.httpx.AsyncClient", return_value=mock_http):
        result = await client.verify_auth()

    assert result.valid is True
    assert result.user_id == "u1"


@pytest.mark.asyncio
async def test_client_maps_401_from_network() -> None:
    client = ZernioClient(alias="t1", api_key="sk_bad")
    mock_response = _response(
        401, json_body={"type": "authentication_error", "code": "bad"}
    )
    mock_http = MagicMock()
    mock_http.request = AsyncMock(return_value=mock_response)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.integrations.social.zernio.httpx.AsyncClient", return_value=mock_http),
        pytest.raises(ProviderAuthError),
    ):
        await client.verify_auth()
