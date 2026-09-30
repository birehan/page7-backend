"""ZernioClient against the local fake Starlette server (401/402/429/ok)."""

from __future__ import annotations

import httpx
import pytest

from app.integrations.errors import (
    ProviderAuthError,
    ProviderPaymentRequiredError,
    ProviderRateLimitedError,
)
from app.integrations.social.zernio import ZernioClient
from tests.fakes.zernio_server import create_fake_zernio_app

_BASE = "http://test"


async def _client_for(mode: str) -> tuple[httpx.AsyncClient, ZernioClient]:
    app = create_fake_zernio_app(default_mode=mode)
    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url=_BASE)
    zernio = ZernioClient(
        alias=f"fake-{mode}",
        api_key="sk_test",
        base_url=_BASE,
        client=http,
    )
    return http, zernio


@pytest.mark.asyncio
async def test_fake_429_raises_rate_limited_with_retry_after() -> None:
    http, zernio = await _client_for("429")
    async with http:
        with pytest.raises(ProviderRateLimitedError) as exc_info:
            await zernio.verify_auth()
    assert exc_info.value.retry_after == 2.0


@pytest.mark.asyncio
async def test_fake_402_raises_payment_required() -> None:
    http, zernio = await _client_for("402")
    async with http:
        with pytest.raises(ProviderPaymentRequiredError):
            await zernio.verify_auth()


@pytest.mark.asyncio
async def test_fake_401_raises_auth_error() -> None:
    http, zernio = await _client_for("401")
    async with http:
        with pytest.raises(ProviderAuthError):
            await zernio.verify_auth()


@pytest.mark.asyncio
async def test_fake_ok_verify_auth_valid() -> None:
    http, zernio = await _client_for("ok")
    async with http:
        result = await zernio.verify_auth()
    assert result.valid is True
    assert result.user_id == "fake-user-1"


@pytest.mark.asyncio
async def test_fake_publish_created_and_idempotent_replay() -> None:
    from app.integrations.social.ports import PublishRequest

    app = create_fake_zernio_app(default_mode="ok")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE) as http:
        zernio = ZernioClient(
            alias="fake-publish",
            api_key="sk_test",
            base_url=_BASE,
            client=http,
        )
        req = PublishRequest(
            content="hello",
            platforms=[{"platform": "instagram", "accountId": "acc_fake_1"}],
            metadata={"post_id": "p1", "publication_id": "pub1"},
            idempotency_key="pub:p1:1",
        )
        first = await zernio.publish(req)
        assert first.kind == "created"
        assert first.zernio_post_id is not None
        assert first.platforms[0].status == "published"

        second = await zernio.publish(req)
        assert second.kind == "existing"
        assert second.zernio_post_id == first.zernio_post_id
        assert len(app.state.create_calls) == 2


@pytest.mark.asyncio
async def test_fake_publish_409_duplicate() -> None:
    from app.integrations.social.ports import PublishRequest

    http, zernio = await _client_for("duplicate")
    async with http:
        result = await zernio.publish(
            PublishRequest(
                content="dup",
                platforms=[{"platform": "instagram", "accountId": "a"}],
                idempotency_key="pub:dup:1",
            )
        )
    assert result.kind == "duplicate_conflict"
    assert result.existing_post_id == "z_other_post"


@pytest.mark.asyncio
async def test_fake_analytics_delta_and_baseline() -> None:
    app = create_fake_zernio_app(default_mode="analytics_delta")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE) as http:
        zernio = ZernioClient(
            alias="fake-analytics",
            api_key="sk_test",
            base_url=_BASE,
            client=http,
            analytics_min_interval=0.0,
        )
        delta = await zernio.get_analytics_delta()
        assert len(delta.data) == 1
        assert delta.data[0].post_id == "zp_delta_1"
        assert delta.next_cursor == "cursor_after_1"

        baseline = await zernio.get_analytics_baseline()
        assert baseline.has_analytics_access is True
        assert baseline.accounts[0].follower_count == 1200


@pytest.mark.asyncio
async def test_fake_analytics_cursor_expired() -> None:
    from app.integrations.errors import AnalyticsCursorExpired

    http, zernio = await _client_for("analytics_cursor_expired")
    # Rebuild with zero pacing.
    zernio = ZernioClient(
        alias="fake-cursor",
        api_key="sk_test",
        base_url=_BASE,
        client=http,
        analytics_min_interval=0.0,
    )
    async with http:
        with pytest.raises(AnalyticsCursorExpired):
            await zernio.get_analytics_delta(cursor="stale")


@pytest.mark.asyncio
async def test_fake_inbox_send_idempotent_replay() -> None:
    app = create_fake_zernio_app(default_mode="inbox_ok")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE) as http:
        zernio = ZernioClient(
            alias="fake-inbox",
            api_key="sk_test",
            base_url=_BASE,
            client=http,
        )
        first = await zernio.send_inbox_message(
            conversation_id="conv_1",
            account_id="acc_fake_1",
            message="hello",
            idempotency_key="reply:msg1",
        )
        assert first.kind == "sent"
        assert first.external_message_id is not None

        second = await zernio.send_inbox_message(
            conversation_id="conv_1",
            account_id="acc_fake_1",
            message="hello",
            idempotency_key="reply:msg1",
        )
        assert second.kind == "replayed"
        assert second.idempotent_replayed is True
        assert second.external_message_id == first.external_message_id
        assert len(app.state.inbox_send_calls) == 2


@pytest.mark.asyncio
async def test_fake_inbox_5xx_releases_idempotency_key() -> None:
    app = create_fake_zernio_app(default_mode="inbox_5xx")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=_BASE) as http:
        zernio = ZernioClient(
            alias="fake-inbox-5xx",
            api_key="sk_test",
            base_url=_BASE,
            client=http,
        )
        first = await zernio.send_inbox_message(
            conversation_id="conv_1",
            account_id="acc_fake_1",
            message="hello",
            idempotency_key="reply:ambig",
        )
        assert first.kind == "ambiguous"
        assert "reply:ambig" not in app.state.idempotency_store

