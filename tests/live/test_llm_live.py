"""Live provider smoke tests — skipped unless RUN_LIVE_TESTS=1."""

from __future__ import annotations

import os

import pytest

from app.core.config import get_settings
from app.integrations.llm import get_llm_router
from app.integrations.llm.ports import LLMMessage, StructuredGenerationRequest

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RUN_LIVE_TESTS") != "1",
        reason="Set RUN_LIVE_TESTS=1 to hit real OpenAI/Anthropic accounts",
    ),
]


@pytest.mark.asyncio
async def test_live_caption_generation_schema() -> None:
    settings = get_settings()
    if not settings.llm.openai_api_key and not settings.llm.anthropic_api_key:
        pytest.skip("No LLM API keys configured")
    router = get_llm_router(settings)
    response = await router.run_structured(
        "caption_generation",
        StructuredGenerationRequest(
            messages=[
                LLMMessage(
                    role="user",
                    content=(
                        "Return JSON with variants[].ar/en captions and hashtags "
                        "for a Riyadh cafe brand. One variant only."
                    ),
                )
            ],
            output_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "variants": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "ar": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "caption": {"type": "string"},
                                        "hashtags": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                    },
                                    "required": ["caption", "hashtags"],
                                },
                                "en": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "caption": {"type": "string"},
                                        "hashtags": {
                                            "type": "array",
                                            "items": {"type": "string"},
                                        },
                                    },
                                    "required": ["caption", "hashtags"],
                                },
                                "first_comment": {"type": ["string", "null"]},
                            },
                            "required": ["ar", "en", "first_comment"],
                        },
                    }
                },
                "required": ["variants"],
            },
            schema_name="caption_generation",
        ),
    )
    assert "variants" in response.content
    assert isinstance(response.content["variants"], list)
    assert response.content["variants"]
