from __future__ import annotations

import pytest

from app.core.config import Environment, Settings


def test_fails_closed_in_production_without_a_real_token_key(  # noqa: E501
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Clear ambient env vars — pydantic-settings reads from env even when
    # _env_file=None, so a test run that exports APP_ENCRYPTION_KEY_CURRENT
    # would otherwise satisfy the validator and prevent the raise.
    monkeypatch.delenv("APP_ENCRYPTION_KEY_CURRENT", raising=False)
    monkeypatch.delenv("APP_ENCRYPTION_KEY__k1", raising=False)
    with pytest.raises(ValueError, match="APP_ENCRYPTION_KEY_CURRENT"):
        Settings(app_env=Environment.PRODUCTION, _env_file=None)


def test_development_does_not_require_the_key() -> None:
    settings = Settings(app_env=Environment.DEVELOPMENT, _env_file=None)
    assert settings.app_env is Environment.DEVELOPMENT
