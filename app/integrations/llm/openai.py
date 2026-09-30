from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import openai
from openai import AsyncOpenAI

from app.integrations.errors import (
    ProviderContentFilteredError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.integrations.llm.cost import estimate_cost_usd
from app.integrations.llm.ports import (
    DoneChunk,
    GenerationChunk,
    LLMTool,
    StructuredGenerationRequest,
    StructuredGenerationResponse,
    TokenChunk,
    Usage,
)
from app.integrations.llm.status_errors import map_llm_http_error


class OpenAIProvider:
    """The only file allowed to `import openai` — see `backend/pyproject.toml`."""

    def __init__(self, *, api_key: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key)

    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
        *,
        model: str,
        timeout_seconds: float | None = None,
    ) -> StructuredGenerationResponse:
        mapped_tools = _map_tools(request.tools)
        create_kwargs: dict[str, Any] = {
            "model": model,
            "instructions": request.system,
            "input": _map_messages(request.messages),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": request.schema_name,
                    "schema": request.output_schema,
                    "strict": True,
                }
            },
            "tools": mapped_tools if mapped_tools else openai.Omit(),
            "max_output_tokens": request.max_output_tokens,
            "timeout": timeout_seconds,
        }
        temperature = _temperature_arg(model, request.temperature)
        if temperature is not None:
            create_kwargs["temperature"] = temperature
        try:
            response = await self._client.responses.create(**create_kwargs)
        except openai.APITimeoutError as exc:
            raise ProviderTimeoutError() from exc
        except openai.APIConnectionError as exc:
            raise ProviderTimeoutError() from exc
        except openai.RateLimitError as exc:
            # OpenAI returns 429 for both true rate limits and exhausted credit
            # balance (`insufficient_quota` / `credit_balance_exhausted`). Billing
            # must map to PaymentRequired so the task router can fall over — and
            # so the UI does not say "temporarily unavailable" for a dead wallet.
            mapped = map_llm_http_error(429, str(exc))
            if mapped is not None:
                raise mapped from exc
            raise ProviderRateLimitedError(retry_after=_retry_after(exc)) from exc
        except openai.APIStatusError as exc:
            mapped = map_llm_http_error(exc.status_code, str(exc))
            if mapped is not None:
                raise mapped from exc
            raise

        return _parse_response(response, model=model)

    async def generate_streaming(
        self,
        request: StructuredGenerationRequest,
        *,
        model: str,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[GenerationChunk]:
        mapped_tools = _map_tools(request.tools)
        create_kwargs: dict[str, Any] = {
            "model": model,
            "instructions": request.system,
            "input": _map_messages(request.messages),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": request.schema_name,
                    "schema": request.output_schema,
                    "strict": True,
                }
            },
            "tools": mapped_tools if mapped_tools else openai.Omit(),
            "max_output_tokens": request.max_output_tokens,
            "timeout": timeout_seconds,
            "stream": True,
        }
        temperature = _temperature_arg(model, request.temperature)
        if temperature is not None:
            create_kwargs["temperature"] = temperature
        try:
            stream = await self._client.responses.create(**create_kwargs)
        except openai.APITimeoutError as exc:
            raise ProviderTimeoutError() from exc
        except openai.APIConnectionError as exc:
            raise ProviderTimeoutError() from exc
        except openai.RateLimitError as exc:
            # OpenAI returns 429 for both true rate limits and exhausted credit
            # balance (`insufficient_quota` / `credit_balance_exhausted`). Billing
            # must map to PaymentRequired so the task router can fall over — and
            # so the UI does not say "temporarily unavailable" for a dead wallet.
            mapped = map_llm_http_error(429, str(exc))
            if mapped is not None:
                raise mapped from exc
            raise ProviderRateLimitedError(retry_after=_retry_after(exc)) from exc
        except openai.APIStatusError as exc:
            mapped = map_llm_http_error(exc.status_code, str(exc))
            if mapped is not None:
                raise mapped from exc
            raise

        async for event in stream:
            if event.type == "response.output_text.delta":
                yield TokenChunk(text=event.delta)
            elif event.type == "response.completed":
                parsed = _parse_response(event.response, model=model)
                yield DoneChunk(usage=parsed.usage, content=parsed.content)


def _map_messages(messages: list[Any]) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for message in messages:
        if message.role == "system":
            continue
        content = message.content
        if isinstance(content, str):
            mapped.append({"role": message.role, "content": content})
        else:
            mapped.append({"role": message.role, "content": content})
    return mapped


def _map_tools(tools: list[LLMTool]) -> list[dict[str, Any]]:
    mapped: list[dict[str, Any]] = []
    for tool in tools:
        if tool.name == "web_search":
            entry: dict[str, Any] = {"type": "web_search"}
            entry.update(tool.params)
            # OpenAI requires user_location.type when user_location is present.
            location = entry.get("user_location")
            if isinstance(location, dict) and "type" not in location:
                entry["user_location"] = {**location, "type": "approximate"}
            mapped.append(entry)
    return mapped


def _temperature_arg(model: str, temperature: float | None) -> float | None:
    """gpt-5.5 / gpt-5.6 / gpt-6 Responses models reject `temperature` entirely."""
    if temperature is None:
        return None
    lowered = model.lower()
    if lowered.startswith(("gpt-5.5", "gpt-5.6", "gpt-6")):
        return None
    return temperature


def _retry_after(exc: openai.RateLimitError) -> float | None:
    headers = getattr(exc, "response", None)
    if headers is None:
        return None
    raw = headers.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_response(response: Any, *, model: str) -> StructuredGenerationResponse:
    content: dict[str, Any] | None = None
    citations: list[dict[str, Any]] = []
    search_calls = 0

    for item in response.output:
        item_type = getattr(item, "type", None)
        if item_type == "refusal":
            raise ProviderContentFilteredError(getattr(item, "refusal", "content filtered"))
        if item_type == "web_search_call":
            search_calls += 1
        if item_type != "message":
            continue
        for block in item.content:
            block_type = getattr(block, "type", None)
            if block_type == "refusal":
                raise ProviderContentFilteredError(getattr(block, "refusal", "content filtered"))
            if block_type != "output_text":
                continue
            for annotation in block.annotations:
                if getattr(annotation, "type", None) == "url_citation":
                    citations.append(annotation.model_dump())
            parsed = json.loads(block.text)
            if isinstance(parsed, dict) and parsed.get("type") == "refusal":
                raise ProviderContentFilteredError(str(parsed.get("refusal", "content filtered")))
            content = parsed

    if content is None:
        raise ProviderUnavailableError("OpenAI response contained no structured output")

    usage_raw = response.usage
    prompt_tokens = usage_raw.input_tokens if usage_raw else 0
    completion_tokens = usage_raw.output_tokens if usage_raw else 0
    usage = Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=estimate_cost_usd(
            model, prompt_tokens, completion_tokens, search_calls=search_calls
        ),
        provider_request_id=response.id,
        search_calls=search_calls,
    )
    return StructuredGenerationResponse(
        content=content,
        usage=usage,
        raw=response.model_dump(mode="json"),
        citations=citations,
    )
