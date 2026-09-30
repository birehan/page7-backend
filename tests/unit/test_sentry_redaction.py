from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from app.core.config import Environment
from app.infrastructure.telemetry.sentry import init_sentry


def test_init_sentry_sets_send_default_pii_false_and_before_send() -> None:
    settings = MagicMock()
    settings.app_env = Environment.STAGING
    settings.observability.sentry_dsn = MagicMock()
    settings.observability.sentry_dsn.get_secret_value.return_value = "https://key@example.com/1"

    with patch("app.infrastructure.telemetry.sentry.sentry_sdk.init") as init:
        init_sentry(settings)
        kwargs = dict(init.call_args.kwargs)
        assert kwargs["send_default_pii"] is False
        assert callable(kwargs["before_send"])
        assert callable(kwargs["before_send_transaction"])
        event: dict[str, Any] = {
            "user": {"email": "a@b.com", "id": "1"},
            "extra": {"password": "secret", "ok": True},
        }
        redacted = kwargs["before_send"](event, {})
        assert redacted["user"]["email"].startswith("sha256:")
        assert redacted["extra"]["password"] == "***REDACTED***"  # noqa: S105
        assert redacted["extra"]["ok"] is True
