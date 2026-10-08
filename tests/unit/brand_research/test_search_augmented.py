"""Unit tests for SearchAugmentedResearcher conversation ordering."""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.features.brand_research.ports import ResearchRequest
from app.features.brand_research.search_augmented import SearchAugmentedResearcher
from app.integrations.llm.fakes import FakeLLMProvider
from app.integrations.llm.router import LLMTaskRouter


@pytest.mark.asyncio
async def test_source_url_in_user_message_not_only_system() -> None:
    fake = FakeLLMProvider()
    router = LLMTaskRouter(
        providers={"openai": fake, "anthropic": fake},
        settings=get_settings().llm,
    )
    researcher = SearchAugmentedResearcher(router)
    url = "https://noor-dental.sa/"
    result = await researcher.research(
        ResearchRequest(source_url=url, brand_context={"name": "Noor"})
    )
    assert result.name.confidence is not None
    assert fake.calls
    call = fake.calls[0]
    messages = call["messages"]
    user_msgs = [m for m in messages if m.role == "user"]
    system_msgs = [m for m in messages if m.role == "system"]
    assert user_msgs
    assert url in user_msgs[0].content
    assert all(url not in (m.content if isinstance(m.content, str) else "") for m in system_msgs)
    assert "web_search" in call["tools"]
    assert "web_fetch" in call["tools"]
