from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable
from typing import Any, cast

import anthropic
from anthropic import AsyncAnthropic
from anthropic.types import MessageParam, ToolParam

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


class AnthropicProvider:
    """The only file allowed to `import anthropic` — see `backend/pyproject.toml`."""

    def __init__(self, *, api_key: str) -> None:
        self._client = AsyncAnthropic(api_key=api_key)

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
            "messages": cast(Iterable[MessageParam], _map_messages(request.messages)),
            "max_tokens": request.max_output_tokens or 4096,
            "timeout": timeout_seconds,
        }
        if request.system is not None:
            create_kwargs["system"] = request.system
        if mapped_tools:
            # Server tools + large JSON Schema grammars exceed Anthropic's compile
            # budget ("compiled grammar is too large"). Ask for JSON in-band instead.
            create_kwargs["tools"] = cast(Iterable[ToolParam], mapped_tools)
            create_kwargs["system"] = _system_with_json_schema(
                create_kwargs.get("system"), request.output_schema
            )
            # Tool loops consume output budget before the final JSON object.
            create_kwargs["max_tokens"] = max(request.max_output_tokens or 0, 8192)
        else:
            create_kwargs["output_config"] = {
                "format": {
                    "type": "json_schema",
                    "schema": request.output_schema,
                }
            }
        try:
            response = await self._client.messages.create(**create_kwargs)
        except anthropic.APITimeoutError as exc:
            raise ProviderTimeoutError() from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderTimeoutError() from exc
        except anthropic.RateLimitError as exc:
            raise ProviderRateLimitedError(retry_after=_retry_after(exc)) from exc
        except anthropic.APIStatusError as exc:
            mapped = map_llm_http_error(exc.status_code, str(exc))
            if mapped is not None:
                raise mapped from exc
            raise

        return _parse_response(response, model=model, allow_raw_json=bool(mapped_tools))

    async def generate_streaming(
        self,
        request: StructuredGenerationRequest,
        *,
        model: str,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[GenerationChunk]:
        mapped_tools = _map_tools(request.tools)
        stream_kwargs: dict[str, Any] = {
            "model": model,
            "messages": cast(Iterable[MessageParam], _map_messages(request.messages)),
            "max_tokens": request.max_output_tokens or 4096,
            "output_config": {
                "format": {
                    "type": "json_schema",
                    "schema": request.output_schema,
                }
            },
            "timeout": timeout_seconds,
        }
        if request.system is not None:
            stream_kwargs["system"] = request.system
        if mapped_tools:
            stream_kwargs["tools"] = cast(Iterable[ToolParam], mapped_tools)
        try:
            async with self._client.messages.stream(**stream_kwargs) as stream:
                async for event in stream:
                    if event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            yield TokenChunk(text=delta.text)
                final = await stream.get_final_message()
        except anthropic.APITimeoutError as exc:
            raise ProviderTimeoutError() from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderTimeoutError() from exc
        except anthropic.RateLimitError as exc:
            raise ProviderRateLimitedError(retry_after=_retry_after(exc)) from exc
        except anthropic.APIStatusError as exc:
            mapped = map_llm_http_error(exc.status_code, str(exc))
            if mapped is not None:
                raise mapped from exc
            raise

        parsed = _parse_response(final, model=model)
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
            entry: dict[str, Any] = {
                "type": "web_search_20260318",
                "name": "web_search",
                # Default on 20260209+ is code_execution; brand research calls search
                # directly and does not register that tool.
                "allowed_callers": ["direct"],
            }
            entry.update(_anthropic_web_search_params(tool.params))
            mapped.append(entry)
        elif tool.name == "web_fetch":
            entry = {"type": "web_fetch_20260318", "name": "web_fetch"}
            entry.update(tool.params)
            mapped.append(entry)
    return mapped


def _anthropic_web_search_params(params: dict[str, Any]) -> dict[str, Any]:
    """Normalize port-level tool params to Anthropic's web_search schema.

    Callers (SearchAugmentedResearcher) pass OpenAI-shaped ``filters.allowed_domains``;
    Anthropic rejects ``filters`` and wants top-level ``allowed_domains`` /
    ``blocked_domains`` instead.
    """
    out = dict(params)
    filters = out.pop("filters", None)
    if isinstance(filters, dict):
        if "allowed_domains" in filters and "allowed_domains" not in out:
            out["allowed_domains"] = filters["allowed_domains"]
        if "blocked_domains" in filters and "blocked_domains" not in out:
            out["blocked_domains"] = filters["blocked_domains"]
    return out


def _system_with_json_schema(system: object, schema: dict[str, Any]) -> str:
    schema_json = json.dumps(schema, separators=(",", ":"))
    instruction = (
        "After using tools, respond with a single JSON object that matches this "
        f"schema (no markdown fences):\n{schema_json}"
    )
    if system is None or system == "":
        return instruction
    if isinstance(system, str):
        return f"{system}\n\n{instruction}"
    return instruction


def _retry_after(exc: anthropic.RateLimitError) -> float | None:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_response(
    response: Any, *, model: str, allow_raw_json: bool = False
) -> StructuredGenerationResponse:
    if response.stop_reason == "refusal":
        raise ProviderContentFilteredError("content filtered")

    content: dict[str, Any] | None = None
    citations: list[dict[str, Any]] = []

    for block in response.content:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            try:
                parsed = json.loads(block.text)
            except json.JSONDecodeError as exc:
                if allow_raw_json:
                    extracted = _extract_json_object(block.text)
                    if extracted is not None:
                        content = extracted
                        continue
                    # Empty/non-JSON after tool use → retryable so OpenAI fallback runs.
                    raise ProviderUnavailableError(
                        "Anthropic tool response contained no JSON object"
                    ) from exc
                # Truncation (max_tokens) yields partial JSON. Treat as retryable so the
                # task router can fall back to OpenAI instead of surfacing INTERNAL.
                if response.stop_reason == "max_tokens":
                    raise ProviderUnavailableError(
                        "Anthropic response truncated before complete JSON"
                    ) from exc
                raise
            content = parsed
        elif block_type == "web_search_tool_result":
            citations.extend(_extract_citations(block))

    if content is None:
        raise ProviderUnavailableError("Anthropic response contained no structured output")

    search_calls = 0
    if response.usage and response.usage.server_tool_use:
        search_calls = response.usage.server_tool_use.web_search_requests

    prompt_tokens = response.usage.input_tokens if response.usage else 0
    completion_tokens = response.usage.output_tokens if response.usage else 0
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


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort pull of a top-level JSON object from model text."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_citations(block: Any) -> list[dict[str, Any]]:
    citations: list[dict[str, Any]] = []
    content = getattr(block, "content", None)
    if not isinstance(content, list):
        return citations
    for item in content:
        if hasattr(item, "model_dump"):
            citations.append(item.model_dump())
    return citations
