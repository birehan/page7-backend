from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Literal

from app.core.config import LLMSettings, TaskConfig
from app.integrations.errors import (
    ProviderAuthError,
    ProviderContentFilteredError,
    ProviderPaymentRequiredError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from app.integrations.llm.ports import (
    GenerationChunk,
    LLMProvider,
    LLMTool,
    StructuredGenerationRequest,
    StructuredGenerationResponse,
)

_RETRYABLE = (ProviderTimeoutError, ProviderRateLimitedError, ProviderUnavailableError)
# Billing/auth on the primary provider should fall over to the configured fallback
# once — same-provider micro-retries cannot fix a dead Anthropic credit balance.
_FALLBACKABLE = (*_RETRYABLE, ProviderPaymentRequiredError, ProviderAuthError)
_BACKOFF_SECONDS = (1.0, 3.0)


class LLMTaskRouter:
    def __init__(self, providers: dict[str, LLMProvider], settings: LLMSettings) -> None:
        self._providers = providers
        self._settings = settings

    async def run_structured(
        self, task: str, request: StructuredGenerationRequest
    ) -> StructuredGenerationResponse:
        cfg = self._resolve_task(task)
        effective = self._effective_request(request, cfg)
        try:
            return await self._with_retries(
                cfg.provider,
                cfg.model,
                cfg,
                lambda provider, model, timeout_seconds: provider.generate_structured(
                    effective, model=model, timeout_seconds=timeout_seconds
                ),
            )
        except _FALLBACKABLE:
            if cfg.fallback_provider is None or cfg.fallback_model is None:
                raise
            fallback_provider = self._providers.get(cfg.fallback_provider)
            if fallback_provider is None:
                raise
            return await fallback_provider.generate_structured(
                effective,
                model=cfg.fallback_model,
                timeout_seconds=cfg.timeout_seconds,
            )

    async def run_streaming(
        self, task: str, request: StructuredGenerationRequest
    ) -> AsyncIterator[GenerationChunk]:
        cfg = self._resolve_task(task)
        effective = self._effective_request(request, cfg)
        try:
            async for chunk in self._stream_with_retries(cfg, effective):
                yield chunk
        except _FALLBACKABLE:
            if cfg.fallback_provider is None or cfg.fallback_model is None:
                raise
            fallback_provider = self._providers.get(cfg.fallback_provider)
            if fallback_provider is None:
                raise
            async for chunk in fallback_provider.generate_streaming(
                effective,
                model=cfg.fallback_model,
                timeout_seconds=cfg.timeout_seconds,
            ):
                yield chunk

    def _resolve_task(self, task: str) -> TaskConfig:
        try:
            return self._settings.tasks[task]
        except KeyError as exc:
            raise KeyError(f"unknown LLM task: {task}") from exc

    def _effective_request(
        self, request: StructuredGenerationRequest, cfg: TaskConfig
    ) -> StructuredGenerationRequest:
        tools = list(request.tools)
        existing = {tool.name for tool in tools}
        for tool_name in cfg.requires_tools:
            if tool_name not in existing:
                tools.append(LLMTool(name=tool_name))
        return request.model_copy(
            update={
                "temperature": cfg.temperature,
                "max_output_tokens": cfg.max_output_tokens or request.max_output_tokens,
                "tools": tools,
            }
        )

    async def _with_retries(
        self,
        provider_name: Literal["openai", "anthropic"],
        model: str,
        cfg: TaskConfig,
        call: Callable[
            [LLMProvider, str, float],
            Awaitable[StructuredGenerationResponse],
        ],
    ) -> StructuredGenerationResponse:
        provider = self._providers[provider_name]
        attempt = 0
        while True:
            try:
                return await call(provider, model, cfg.timeout_seconds)
            except ProviderContentFilteredError:
                raise
            except ProviderRateLimitedError as exc:
                if attempt >= cfg.max_retries:
                    raise
                await self._sleep_before_retry(exc.retry_after, attempt)
                attempt += 1
            except (ProviderTimeoutError, ProviderUnavailableError):
                if attempt >= cfg.max_retries:
                    raise
                await self._sleep_before_retry(None, attempt)
                attempt += 1

    async def _stream_with_retries(
        self, cfg: TaskConfig, request: StructuredGenerationRequest
    ) -> AsyncIterator[GenerationChunk]:
        provider = self._providers[cfg.provider]
        attempt = 0
        while True:
            try:
                async for chunk in provider.generate_streaming(
                    request, model=cfg.model, timeout_seconds=cfg.timeout_seconds
                ):
                    yield chunk
                return
            except ProviderContentFilteredError:
                raise
            except ProviderRateLimitedError as exc:
                if attempt >= cfg.max_retries:
                    raise
                await self._sleep_before_retry(exc.retry_after, attempt)
                attempt += 1
            except (ProviderTimeoutError, ProviderUnavailableError):
                if attempt >= cfg.max_retries:
                    raise
                await self._sleep_before_retry(None, attempt)
                attempt += 1

    async def _sleep_before_retry(self, retry_after: float | None, attempt: int) -> None:
        if retry_after is not None:
            delay = retry_after
        else:
            delay = _BACKOFF_SECONDS[min(attempt, len(_BACKOFF_SECONDS) - 1)]
        delay += random.uniform(0, 0.5)  # noqa: S311
        await asyncio.sleep(delay)
