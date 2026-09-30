"""Anthropic structured-output parsing edge cases."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.integrations.errors import ProviderUnavailableError
from app.integrations.llm.anthropic import _anthropic_web_search_params, _map_tools, _parse_response
from app.integrations.llm.ports import LLMTool


def test_parse_response_max_tokens_truncation_is_retryable() -> None:
    response = SimpleNamespace(
        id="msg_truncated",
        stop_reason="max_tokens",
        content=[SimpleNamespace(type="text", text='{"items":[{"title":"partial')],
        usage=SimpleNamespace(input_tokens=10, output_tokens=100, server_tool_use=None),
    )
    with pytest.raises(ProviderUnavailableError, match="truncated"):
        _parse_response(response, model="claude-sonnet-5")


def test_map_tools_hoists_openai_shaped_domain_filters() -> None:
    mapped = _map_tools(
        [
            LLMTool(
                name="web_search",
                params={
                    "filters": {"allowed_domains": ["jarir.com", "instagram.com"]},
                    "user_location": {"type": "approximate", "country": "SA"},
                },
            )
        ]
    )
    assert mapped == [
        {
            "type": "web_search_20260318",
            "name": "web_search",
            "allowed_callers": ["direct"],
            "allowed_domains": ["jarir.com", "instagram.com"],
            "user_location": {"type": "approximate", "country": "SA"},
        }
    ]


def test_anthropic_web_search_params_drops_empty_filters() -> None:
    assert _anthropic_web_search_params({"filters": {}, "max_uses": 3}) == {"max_uses": 3}
