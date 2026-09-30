"""FakeSocialProvider records calls and returns deterministic IDs."""

from __future__ import annotations

import pytest

from app.core.config import Environment, Settings, SocialSettings
from app.integrations.social import (
    FakeSocialProvider,
    get_social_provider,
)


@pytest.mark.asyncio
async def test_fake_social_provider_records_calls() -> None:
    provider = FakeSocialProvider()

    auth = await provider.verify_auth()
    assert auth.valid is True
    assert provider.calls[0][0] == "verify_auth"

    profile = await provider.create_profile("Brand A", description="test")
    assert profile.id.startswith("prof_1_")
    assert profile.name == "Brand A"

    connect = await provider.get_connect_url(
        "instagram",
        profile.id,
        "http://localhost:8000/v1/integrations/zernio/callback?state=abc",
        login_method="instagram_login",
    )
    assert "connected=instagram" in connect.auth_url
    assert "profileId=" in connect.auth_url
    assert "accountId=" in connect.auth_url
    assert "zernio.test" not in connect.auth_url
    assert connect.auth_url.startswith("http://localhost:8000/v1/integrations/zernio/callback")
    assert connect.provider_state.startswith("state_instagram_")

    # get_connect_url seeds a provisional account for sync/connect tests.
    accounts = await provider.list_accounts(profile_id=profile.id)
    assert len(accounts) == 1
    account = accounts[0]

    health = await provider.get_accounts_health(profile_id=profile.id)
    assert health[0].account_id == account.id
    assert health[0].token_valid is True

    valid = await provider.validate_post({"content": "hi", "platforms": []})
    assert valid.valid is True

    await provider.delete_account(account.id)
    assert await provider.list_accounts() == []

    methods = [name for name, _ in provider.calls]
    assert "create_profile" in methods
    assert "get_connect_url" in methods
    assert "delete_account" in methods


@pytest.mark.asyncio
async def test_fake_publish_is_idempotent_on_same_key() -> None:
    from app.integrations.social.ports import PublishRequest

    provider = FakeSocialProvider()
    req = PublishRequest(
        content="hello",
        platforms=[{"platform": "instagram", "accountId": "acct_1"}],
        metadata={"post_id": "p1"},
        idempotency_key="pub:p1:1",
    )
    first = await provider.publish(req)
    second = await provider.publish(req)
    assert first.kind == "created"
    assert second.kind == "existing"
    assert first.zernio_post_id == second.zernio_post_id
    assert provider.publish_call_count() == 2


@pytest.mark.asyncio
async def test_fake_publish_script_rate_limited() -> None:
    from app.integrations.social.ports import PublishRequest

    provider = FakeSocialProvider()
    provider.publish_script = "rate_limited"
    result = await provider.publish(
        PublishRequest(
            content="x",
            platforms=[{"platform": "instagram", "accountId": "a"}],
            idempotency_key="pub:x:1",
        )
    )
    assert result.kind == "rate_limited"
    assert result.retry_after == 60.0


def test_factory_resolves_to_fake_under_development() -> None:
    settings = Settings(app_env=Environment.DEVELOPMENT, _env_file=None)
    provider = get_social_provider(settings, alias="t1")
    assert isinstance(provider, FakeSocialProvider)


def test_factory_auto_uses_zernio_when_keys_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.integrations.social import ZernioClient

    monkeypatch.setenv("ZERNIO_API_KEY__t1", "sk_test_not_a_placeholder")
    settings = Settings(
        app_env=Environment.DEVELOPMENT,
        social=SocialSettings(provider="auto"),
        _env_file=None,
    )
    provider = get_social_provider(settings, alias="t1")
    assert isinstance(provider, ZernioClient)


def test_factory_resolves_to_fake_when_explicit() -> None:
    settings = Settings(
        app_env=Environment.PRODUCTION,
        app_encryption_key_current="k1",
        app_encryption_key={"k1": "a" * 43},
        social=SocialSettings(provider="fake"),
        _env_file=None,
    )
    provider = get_social_provider(settings, alias="t1")
    assert isinstance(provider, FakeSocialProvider)
