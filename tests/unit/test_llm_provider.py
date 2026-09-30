from __future__ import annotations

import pytest

from app.core.config import Environment, LLMSettings, Settings
from app.integrations.llm import get_llm_router
from app.integrations.llm.anthropic import AnthropicProvider
from app.integrations.llm.fakes import FakeLLMProvider
from app.integrations.llm.openai import OpenAIProvider


def test_resolves_to_fake_under_development(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(app_env=Environment.DEVELOPMENT, _env_file=None)  # type: ignore[call-arg]
    router = get_llm_router(settings)
    assert isinstance(router._providers["openai"], FakeLLMProvider)  # noqa: SLF001
    assert isinstance(router._providers["anthropic"], FakeLLMProvider)  # noqa: SLF001


def test_raises_without_any_api_key_outside_local_envs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # architecture/06 §2's fail-closed rule must hold for LLM the same way it
    # already does for storage (R2, see test_storage_provider.py) and email
    # (Resend, see test_email_provider.py) — a misconfigured non-local deploy
    # must error loudly, not silently fall back to the fake provider and
    # serve canned content as if it were real.
    #
    # Clear ambient env vars — pydantic-settings reads .env even when passing
    # an explicit `llm=` kwarg, so a local checkout with real LLM__* keys set
    # would otherwise satisfy the check and mask this test's real intent.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM__OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM__ANTHROPIC_API_KEY", raising=False)
    # conftest.py force-sets LLM__PROVIDER=fake for the whole non-live suite
    # (env vars win over nested-model init kwargs in pydantic-settings) —
    # override it back to "auto" so this test actually exercises the
    # fail-closed path it's named for.
    monkeypatch.setenv("LLM__PROVIDER", "auto")
    settings = Settings(
        app_env=Environment.PRODUCTION,
        app_encryption_key_current="k1",
        app_encryption_key={"k1": "a" * 43},
        llm=LLMSettings(provider="auto"),
        _env_file=None,  # type: ignore[call-arg]
    )
    with pytest.raises(
        RuntimeError, match="LLM__OPENAI_API_KEY / LLM__ANTHROPIC_API_KEY"
    ):
        get_llm_router(settings)


def test_resolves_to_real_openai_in_production_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM__OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM__ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM__PROVIDER", "auto")
    settings = Settings(
        app_env=Environment.PRODUCTION,
        app_encryption_key_current="k1",
        app_encryption_key={"k1": "a" * 43},
        llm=LLMSettings(provider="auto", openai_api_key="sk-test"),  # type: ignore[arg-type]
        _env_file=None,  # type: ignore[call-arg]
    )
    router = get_llm_router(settings)
    assert isinstance(router._providers["openai"], OpenAIProvider)  # noqa: SLF001


def test_resolves_to_real_anthropic_in_production_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LLM__OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("LLM__ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("LLM__PROVIDER", "auto")
    settings = Settings(
        app_env=Environment.PRODUCTION,
        app_encryption_key_current="k1",
        app_encryption_key={"k1": "a" * 43},
        llm=LLMSettings(provider="auto", anthropic_api_key="sk-ant-test"),  # type: ignore[arg-type]
        _env_file=None,  # type: ignore[call-arg]
    )
    router = get_llm_router(settings)
    assert isinstance(router._providers["anthropic"], AnthropicProvider)  # noqa: SLF001


def test_explicit_fake_provider_bypasses_the_fail_closed_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # LLM__PROVIDER=fake is an explicit, deliberate test-suite override (see
    # tests/conftest.py) — this must still resolve even outside local envs,
    # or the test suite itself couldn't run non-live tests against a
    # "production"-flagged Settings object.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    settings = Settings(
        app_env=Environment.PRODUCTION,
        app_encryption_key_current="k1",
        app_encryption_key={"k1": "a" * 43},
        llm=LLMSettings(provider="fake"),
        _env_file=None,  # type: ignore[call-arg]
    )
    router = get_llm_router(settings)
    assert isinstance(router._providers["openai"], FakeLLMProvider)  # noqa: SLF001
