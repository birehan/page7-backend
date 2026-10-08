"""Unit tests for LLM HTTP status → ProviderError mapping and payment fallback."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.core.config import LLMSettings
from app.features.content_ai.service import _provider_error_code
from app.integrations.errors import (
    ProviderAuthError,
    ProviderPaymentRequiredError,
    ProviderUnavailableError,
)
from app.integrations.llm.ports import (
    LLMMessage,
    StructuredGenerationRequest,
    StructuredGenerationResponse,
    Usage,
)
from app.integrations.llm.router import LLMTaskRouter
from app.integrations.llm.status_errors import map_llm_http_error


def test_provider_error_code_hides_billing_details() -> None:
    code, message = _provider_error_code(
        ProviderPaymentRequiredError("credit balance too low — top up Anthropic")
    )
    assert code == "PROVIDER_UNAVAILABLE"
    assert message == "The AI provider is temporarily unavailable"
    assert "credit" not in message.lower()
    assert "anthropic" not in message.lower()
    assert "openai" not in message.lower()


def test_map_credit_balance_400_is_payment_required() -> None:
    mapped = map_llm_http_error(
        400,
        'Error code: 400 - {"type": "error", "error": {"type": "invalid_request_error", '
        '"message": "Your credit balance is too low to access the Anthropic API."}}',
    )
    assert isinstance(mapped, ProviderPaymentRequiredError)


def test_map_ordinary_400_is_not_remapped() -> None:
    assert map_llm_http_error(400, "invalid schema: missing required") is None


def test_map_402_and_401() -> None:
    assert isinstance(map_llm_http_error(402, "payment required"), ProviderPaymentRequiredError)
    assert isinstance(map_llm_http_error(401, "bad key"), ProviderAuthError)


def test_map_5xx_is_unavailable() -> None:
    assert isinstance(map_llm_http_error(503, "overload"), ProviderUnavailableError)


class _AlwaysFails:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
        *,
        model: str,
        timeout_seconds: float | None = None,
    ) -> StructuredGenerationResponse:
        self.calls.append({"model": model, "timeout_seconds": timeout_seconds})
        raise self.error


class _AlwaysSucceeds:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
        *,
        model: str,
        timeout_seconds: float | None = None,
    ) -> StructuredGenerationResponse:
        self.calls.append({"model": model, "timeout_seconds": timeout_seconds})
        return StructuredGenerationResponse(
            content={"ok": True},
            usage=Usage(
                prompt_tokens=1,
                completion_tokens=1,
                cost_usd=Decimal("0"),
                provider_request_id="fb",
            ),
            raw={},
        )


@pytest.mark.asyncio
async def test_router_falls_back_on_primary_payment_required() -> None:
    """Fallback still works when a task explicitly configures one (defaults do not)."""
    from app.core.config import TaskConfig

    tasks = dict(LLMSettings().tasks)
    tasks["brand_research"] = TaskConfig(
        provider="openai",
        model="gpt-5.5",
        timeout_seconds=45,
        max_retries=1,
        temperature=0.2,
        fallback_provider="openai",
        fallback_model="gpt-5.6-luna",
    )

    class _FailThenOk:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def generate_structured(self, request, *, model, timeout_seconds=None):
            self.calls.append({"model": model, "timeout_seconds": timeout_seconds})
            if model == "gpt-5.5":
                raise ProviderPaymentRequiredError("credit balance too low")
            return StructuredGenerationResponse(
                content={"ok": True},
                usage=Usage(
                    prompt_tokens=1,
                    completion_tokens=1,
                    cost_usd=Decimal("0"),
                    provider_request_id="fb",
                ),
                raw={},
            )

    provider = _FailThenOk()
    router = LLMTaskRouter(
        providers={"openai": provider},  # type: ignore[dict-item]
        settings=LLMSettings(tasks=tasks),
    )
    response = await router.run_structured(
        "brand_research",
        StructuredGenerationRequest(
            messages=[LLMMessage(role="user", content="hi")],
            output_schema={"type": "object", "properties": {}, "additionalProperties": False},
            schema_name="brand_research",
        ),
    )
    assert response.content == {"ok": True}
    assert [c["model"] for c in provider.calls] == ["gpt-5.5", "gpt-5.6-luna"]
