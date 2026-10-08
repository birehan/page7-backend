"""Unit tests for LLM task router settings validation and cost table."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.config import LLMSettings, TaskConfig
from app.integrations.llm.cost import estimate_cost_usd
from app.integrations.llm.fakes import FakeLLMProvider
from app.integrations.llm.ports import LLMMessage, StructuredGenerationRequest
from app.integrations.llm.router import LLMTaskRouter


def test_estimate_cost_usd_gpt55() -> None:
    cost = estimate_cost_usd("gpt-5.5", prompt_tokens=1_000_000, completion_tokens=0)
    assert cost == Decimal("5")


def test_estimate_cost_usd_includes_search_surcharge() -> None:
    cost = estimate_cost_usd(
        "gpt-5.5",
        prompt_tokens=0,
        completion_tokens=0,
        search_calls=1000,
    )
    assert cost == Decimal("10")


def test_task_requiring_tools_rejects_incapable_model() -> None:
    with pytest.raises(ValidationError):
        LLMSettings(
            tasks={
                "brand_research": TaskConfig(
                    provider="openai",
                    model="gpt-5.6-luna",
                    requires_tools=["web_search"],
                )
            }
        )


def test_default_llm_settings_load() -> None:
    settings = LLMSettings()
    assert "caption_generation" in settings.tasks
    assert settings.tasks["caption_generation"].provider == "openai"
    assert settings.tasks["caption_generation"].model == "gpt-5.5"
    assert settings.tasks["caption_generation"].fallback_provider is None
    assert settings.tasks["caption_transform"].provider == "openai"
    assert settings.tasks["caption_transform"].model == "gpt-5.6-luna"
    assert settings.tasks["classification"].provider == "openai"
    assert settings.tasks["classification"].model == "gpt-5.6-luna"
    assert settings.tasks["insight_report"].provider == "openai"
    assert settings.tasks["insight_report"].model == "gpt-5.5"
    assert settings.tasks["brand_research"].provider == "openai"
    assert settings.tasks["brand_research"].model == "gpt-5.5"
    plan = settings.tasks["plan_generation"]
    assert plan.provider == "openai"
    assert plan.model == "gpt-5.5"
    assert plan.timeout_seconds == 90
    assert plan.max_output_tokens == 16384


@pytest.mark.asyncio
async def test_router_uses_fake_structured_response() -> None:
    fake = FakeLLMProvider()
    settings = LLMSettings()
    router = LLMTaskRouter(
        providers={"openai": fake, "anthropic": fake},
        settings=settings,
    )
    response = await router.run_structured(
        "caption_generation",
        StructuredGenerationRequest(
            messages=[LLMMessage(role="user", content="hi")],
            output_schema={"type": "object", "properties": {}, "additionalProperties": False},
            schema_name="caption_generation",
        ),
    )
    assert "variants" in response.content
    assert fake.calls
