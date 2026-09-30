from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class LLMMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str | list[dict[str, Any]]


class LLMTool(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: Literal["web_search", "web_fetch"]
    params: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int
    completion_tokens: int
    cost_usd: Decimal
    provider_request_id: str | None = None
    search_calls: int = 0


class StructuredGenerationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: list[LLMMessage]
    output_schema: dict[str, Any]
    schema_name: str
    temperature: float = 0.7
    max_output_tokens: int | None = None
    tools: list[LLMTool] = Field(default_factory=list)
    system: str | None = None


class StructuredGenerationResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    content: dict[str, Any]
    usage: Usage
    raw: dict[str, Any] | None = None
    citations: list[dict[str, Any]] = Field(default_factory=list)


class TokenChunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["token"] = "token"
    text: str


class DoneChunk(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["done"] = "done"
    usage: Usage
    content: dict[str, Any] | None = None


GenerationChunk = TokenChunk | DoneChunk


@runtime_checkable
class LLMProvider(Protocol):
    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
        *,
        model: str,
        timeout_seconds: float | None = None,
    ) -> StructuredGenerationResponse: ...

    def generate_streaming(
        self,
        request: StructuredGenerationRequest,
        *,
        model: str,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[GenerationChunk]: ...
