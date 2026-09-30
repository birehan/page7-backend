from __future__ import annotations

import os
from typing import Annotated, cast

from fastapi import Depends
from pydantic import SecretStr

from app.core.config import Environment, Settings, get_settings
from app.integrations.llm.anthropic import AnthropicProvider
from app.integrations.llm.fakes import FakeLLMProvider
from app.integrations.llm.openai import OpenAIProvider
from app.integrations.llm.ports import LLMProvider
from app.integrations.llm.router import LLMTaskRouter

_LOCAL_ENVS = (Environment.DEVELOPMENT, Environment.TESTING)


def _anthropic_api_key(settings: Settings) -> SecretStr | None:
    """Prefer LLM__ANTHROPIC_API_KEY; accept bare ANTHROPIC_API_KEY as alias."""
    if settings.llm.anthropic_api_key is not None:
        return settings.llm.anthropic_api_key
    raw = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if raw:
        return SecretStr(raw)
    return None


def get_llm_router(settings: Annotated[Settings, Depends(get_settings)]) -> LLMTaskRouter:
    """architecture/06 §2: which adapter answers for a port is a configuration value.

    Mirrors storage's/email's fail-closed shape exactly: `auto` only resolves
    to the fake provider inside local envs. Outside them, `auto` requires a
    real key or raises — it must never silently serve canned AI content in
    a misconfigured non-local deploy just because no key happened to be set.
    """
    anthropic_key = _anthropic_api_key(settings)
    use_fake = settings.llm.provider == "fake" or (
        settings.llm.provider == "auto" and settings.app_env in _LOCAL_ENVS
    )
    if use_fake:
        fake = FakeLLMProvider()
        return LLMTaskRouter(
            providers=cast(dict[str, LLMProvider], {"openai": fake, "anthropic": fake}),
            settings=settings.llm,
        )

    providers: dict[str, OpenAIProvider | AnthropicProvider] = {}
    openai_key = settings.llm.openai_api_key
    if openai_key is not None:
        providers["openai"] = OpenAIProvider(api_key=openai_key.get_secret_value())
    if anthropic_key is not None:
        providers["anthropic"] = AnthropicProvider(api_key=anthropic_key.get_secret_value())

    if not providers:
        raise RuntimeError(
            "LLM__OPENAI_API_KEY / LLM__ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY) "
            "must be configured"
        )

    return LLMTaskRouter(
        providers=cast(dict[str, LLMProvider], providers),
        settings=settings.llm,
    )
