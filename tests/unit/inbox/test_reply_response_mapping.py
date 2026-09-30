"""Inbox send response mapping — 2xx / 409 / 422 / 5xx / Idempotent-Replayed."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.social.fakes import FakeSocialProvider
from app.integrations.social.zernio import ZernioClient, _normalize_inbox_send


def test_normalize_sent() -> None:
    headers = httpx.Headers({})
    result = _normalize_inbox_send(201, {"messageId": "m1"}, headers)
    assert result.kind == "sent"
    assert result.external_message_id == "m1"
    assert result.idempotent_replayed is False


def test_normalize_replayed_header() -> None:
    headers = httpx.Headers({"Idempotent-Replayed": "true"})
    result = _normalize_inbox_send(200, {"_id": "m1"}, headers)
    assert result.kind == "replayed"
    assert result.idempotent_replayed is True


def test_normalize_in_flight_409() -> None:
    result = _normalize_inbox_send(409, {"error": "in flight"}, httpx.Headers({}))
    assert result.kind == "in_flight"
    assert result.http_status == 409


def test_normalize_conflict_422() -> None:
    result = _normalize_inbox_send(422, {"error": "conflict"}, httpx.Headers({}))
    assert result.kind == "conflict"
    assert result.http_status == 422


def test_normalize_ambiguous_5xx() -> None:
    result = _normalize_inbox_send(503, {"error": "boom"}, httpx.Headers({}))
    assert result.kind == "ambiguous"
    assert result.http_status == 503


def test_normalize_failed_4xx() -> None:
    result = _normalize_inbox_send(400, {"error": "bad"}, httpx.Headers({}))
    assert result.kind == "failed"


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
        request=httpx.Request(
            "POST", "https://zernio.com/api/v1/inbox/conversations/c1/messages"
        ),
    )


@pytest.mark.asyncio
async def test_client_send_maps_409_to_in_flight() -> None:
    client = ZernioClient(alias="t-inbox-409", api_key="sk_test")
    mock_response = _response(409, json_body={"error": "in flight"})
    mock_http = MagicMock()
    mock_http.request = AsyncMock(return_value=mock_response)
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)

    with patch("app.integrations.social.zernio.httpx.AsyncClient", return_value=mock_http):
        result = await client.send_inbox_message(
            conversation_id="c1",
            account_id="a1",
            message="hi",
            idempotency_key="reply:1",
        )
    assert result.kind == "in_flight"


@pytest.mark.asyncio
async def test_client_send_timeout_is_ambiguous_not_raised() -> None:
    client = ZernioClient(alias="t-inbox-timeout", api_key="sk_test")
    mock_http = MagicMock()
    mock_http.request = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)

    with patch("app.integrations.social.zernio.httpx.AsyncClient", return_value=mock_http):
        result = await client.send_inbox_message(
            conversation_id="c1",
            account_id="a1",
            message="hi",
            idempotency_key="reply:1",
        )
    assert result.kind == "ambiguous"


@pytest.mark.asyncio
async def test_fake_reply_scripts() -> None:
    provider = FakeSocialProvider()

    provider.inbox_reply_script = "sent"
    sent = await provider.reply_to_post_comment(
        post_id="p1",
        account_id="a1",
        message="thanks",
        idempotency_key="k-sent",
    )
    assert sent.kind == "sent"
    assert provider.send_call_count == 1

    replayed_same = await provider.reply_to_post_comment(
        post_id="p1",
        account_id="a1",
        message="thanks",
        idempotency_key="k-sent",
    )
    assert replayed_same.kind == "replayed"
    assert provider.send_call_count == 1

    provider.inbox_reply_script = "replayed"
    replayed = await provider.send_inbox_message(
        conversation_id="c1",
        account_id="a1",
        message="hi",
        idempotency_key="k-replay",
    )
    assert replayed.kind == "replayed"
    assert replayed.idempotent_replayed is True

    provider.inbox_reply_script = "in_flight_409"
    in_flight = await provider.send_inbox_message(
        conversation_id="c1",
        account_id="a1",
        message="hi",
        idempotency_key="k-flight",
    )
    assert in_flight.kind == "in_flight"

    provider.inbox_reply_script = "conflict_422"
    conflict = await provider.send_inbox_message(
        conversation_id="c1",
        account_id="a1",
        message="hi",
        idempotency_key="k-conflict",
    )
    assert conflict.kind == "conflict"

    provider.inbox_reply_script = "ambiguous_5xx_after_accept"
    before = provider.send_call_count
    first = await provider.send_inbox_message(
        conversation_id="c1",
        account_id="a1",
        message="hi",
        idempotency_key="k-ambig",
    )
    second = await provider.send_inbox_message(
        conversation_id="c1",
        account_id="a1",
        message="hi",
        idempotency_key="k-ambig",
    )
    assert first.kind == "ambiguous"
    assert second.kind == "ambiguous"
    # Blind retry increments — would double-send if the first call actually landed.
    assert provider.send_call_count == before + 2
