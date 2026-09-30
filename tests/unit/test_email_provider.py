from __future__ import annotations

from typing import Any

import pytest
import resend
from resend.exceptions import RateLimitError, ResendError

from app.core.config import EmailSettings, Environment, Settings
from app.integrations.email import get_email_provider
from app.integrations.email.console import ConsoleEmailProvider
from app.integrations.email.fakes import FakeEmailProvider
from app.integrations.email.ports import EmailMessage
from app.integrations.email.resend import ResendEmailProvider
from app.integrations.email.smtp import SmtpEmailProvider
from app.integrations.errors import ProviderRateLimitedError, ProviderUnavailableError

_MESSAGE = EmailMessage(
    to="user@example.com",
    subject="Test",
    html="<p>hi</p>",
    text="hi",
    idempotency_key="invite:123",
)


async def test_fake_email_provider_records_sent_messages() -> None:
    provider = FakeEmailProvider()
    await provider.send(_MESSAGE)
    assert provider.sent == [_MESSAGE]


def test_factory_resolves_to_console_under_development() -> None:
    settings = Settings(app_env=Environment.DEVELOPMENT, _env_file=None)
    assert isinstance(get_email_provider(settings), ConsoleEmailProvider)


def test_factory_resolves_to_console_when_explicitly_configured() -> None:
    settings = Settings(
        app_env=Environment.PRODUCTION,
        app_encryption_key_current="k1",
        app_encryption_key={"k1": "a" * 43},
        email=EmailSettings(provider="console"),
        _env_file=None,
    )
    assert isinstance(get_email_provider(settings), ConsoleEmailProvider)


def test_factory_resolves_to_smtp_when_configured() -> None:
    settings = Settings(
        app_env=Environment.DEVELOPMENT,
        email=EmailSettings(
            provider="smtp",
            smtp_password="secret",
        ),
        _env_file=None,
    )
    provider = get_email_provider(settings)
    assert isinstance(provider, SmtpEmailProvider)


def test_factory_raises_without_smtp_password() -> None:
    settings = Settings(
        app_env=Environment.DEVELOPMENT,
        email=EmailSettings(provider="smtp"),
        _env_file=None,
    )
    with pytest.raises(RuntimeError, match="EMAIL__SMTP_PASSWORD"):
        get_email_provider(settings)


def test_factory_resolves_to_resend_in_production_when_configured() -> None:
    settings = Settings(
        app_env=Environment.PRODUCTION,
        app_encryption_key_current="k1",
        app_encryption_key={"k1": "a" * 43},
        email=EmailSettings(provider="auto", resend_api_key="re_test_key"),
        _env_file=None,
    )
    assert isinstance(get_email_provider(settings), ResendEmailProvider)


def test_factory_raises_without_a_resend_api_key_outside_local_envs() -> None:
    settings = Settings(
        app_env=Environment.PRODUCTION,
        app_encryption_key_current="k1",
        app_encryption_key={"k1": "a" * 43},
        email=EmailSettings(provider="auto"),
        _env_file=None,
    )
    with pytest.raises(RuntimeError, match="EMAIL__RESEND_API_KEY"):
        get_email_provider(settings)


async def test_resend_provider_maps_rate_limit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise RateLimitError(code=429, error_type="rate_limit_exceeded", message="slow down")

    monkeypatch.setattr(resend.Emails, "send_async", _raise)
    provider = ResendEmailProvider(api_key="re_test", from_address="Page7 <contact@page7.io>")

    with pytest.raises(ProviderRateLimitedError):
        await provider.send(_MESSAGE)


async def test_resend_provider_maps_other_errors_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise(*_args: Any, **_kwargs: Any) -> None:
        raise ResendError(
            code=500, error_type="internal_server_error", message="boom", suggested_action=""
        )

    monkeypatch.setattr(resend.Emails, "send_async", _raise)
    provider = ResendEmailProvider(api_key="re_test", from_address="Page7 <contact@page7.io>")

    with pytest.raises(ProviderUnavailableError):
        await provider.send(_MESSAGE)
