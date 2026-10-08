from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from typing import Any

from app.integrations.errors import ProviderContentFilteredError
from app.integrations.llm.ports import (
    DoneChunk,
    GenerationChunk,
    StructuredGenerationRequest,
    StructuredGenerationResponse,
    TokenChunk,
    Usage,
)


class FakeLLMProvider:
    """Deterministic canned responses, zero network I/O (architecture/06 §5)."""

    def __init__(
        self,
        *,
        fail_validation_once: bool = False,
        content_filter: bool = False,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail_validation_once = fail_validation_once
        self.content_filter = content_filter
        self._validation_failures_remaining = 1 if fail_validation_once else 0

    async def generate_structured(
        self,
        request: StructuredGenerationRequest,
        *,
        model: str,
        timeout_seconds: float | None = None,
    ) -> StructuredGenerationResponse:
        self.calls.append(
            {
                "method": "generate_structured",
                "model": model,
                "schema_name": request.schema_name,
                "messages": request.messages,
                "tools": [t.name for t in request.tools],
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.content_filter:
            raise ProviderContentFilteredError("fake content filter")
        if self._validation_failures_remaining > 0:
            self._validation_failures_remaining -= 1
            content: dict[str, Any] = {"invalid": True}
        else:
            content = self._content_for(request.schema_name)
        usage = Usage(
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=Decimal("0.001"),
            provider_request_id="fake-req-id",
        )
        return StructuredGenerationResponse(content=content, usage=usage, raw={"fake": True})

    async def generate_streaming(
        self,
        request: StructuredGenerationRequest,
        *,
        model: str,
        timeout_seconds: float | None = None,
    ) -> AsyncIterator[GenerationChunk]:
        self.calls.append(
            {
                "method": "generate_streaming",
                "model": model,
                "schema_name": request.schema_name,
                "messages": request.messages,
                "timeout_seconds": timeout_seconds,
            }
        )
        if self.content_filter:
            raise ProviderContentFilteredError("fake content filter")

        content = self._content_for(request.schema_name)
        text_blob = str(content.get("variants", content))
        for word in text_blob.split():
            yield TokenChunk(text=f"{word} ")
        usage = Usage(
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=Decimal("0.001"),
            provider_request_id="fake-req-id",
        )
        yield DoneChunk(usage=usage, content=content)

    def _content_for(self, schema_name: str) -> dict[str, Any]:
        name = schema_name.lower()
        if "caption" in name:
            return {
                "variants": [
                    {
                        "ar": {
                            "caption": "نص تجريبي بالعربية",
                            "hashtags": ["#تجربة", "#السعودية"],
                        },
                        "en": {
                            "caption": "Sample caption in English",
                            "hashtags": ["#pgblank", "#marketing"],
                        },
                        "first_comment": "تابعونا للمزيد",
                    },
                    {
                        "ar": {
                            "caption": "نسخة ثانية بالعربية",
                            "hashtags": ["#علامة", "#تسويق"],
                        },
                        "en": {
                            "caption": "Second sample caption",
                            "hashtags": ["#brand", "#social"],
                        },
                        "first_comment": None,
                    },
                    {
                        "ar": {
                            "caption": "نسخة ثالثة بالعربية",
                            "hashtags": ["#رياض"],
                        },
                        "en": {
                            "caption": "Third sample caption",
                            "hashtags": ["#Riyadh"],
                        },
                        "first_comment": None,
                    },
                ]
            }
        if "plan" in name:
            return {
                "items": [
                    {
                        "title": "Launch teaser",
                        "platform": "instagram",
                        "day_offset": 0,
                        "scheduled_time": "14:00",
                        "text_ar": "إطلاق جديد",
                        "text_en": "New launch",
                        "hashtags_ar": ["#جديد"],
                        "hashtags_en": ["#new"],
                        "pillar_index": 0,
                    },
                    {
                        "title": "Product highlight",
                        "platform": "instagram",
                        "day_offset": 2,
                        "scheduled_time": "11:00",
                        "text_ar": "منتج مميز",
                        "text_en": "Product highlight",
                        "hashtags_ar": ["#منتج"],
                        "hashtags_en": ["#product"],
                        "pillar_index": 0,
                    },
                    {
                        "title": "Community post",
                        "platform": "facebook",
                        "day_offset": 4,
                        "scheduled_time": "16:00",
                        "text_ar": "مجتمعنا",
                        "text_en": "Our community",
                        "hashtags_ar": ["#مجتمع"],
                        "hashtags_en": ["#community"],
                        "pillar_index": 0,
                    },
                ]
            }
        if "strategy" in name:
            return {
                "goals": [
                    {"text": "Grow awareness", "progress": 0},
                    {"text": "Increase engagement", "progress": 0},
                ],
                "cadence": {
                    "instagram": 4,
                    "facebook": 2,
                    "tiktok": 1,
                    "snapchat": 1,
                    "whatsapp": 0,
                },
            }
        if "alt" in name:
            return {
                "alt_ar": "صورة لمنتج على خلفية بيضاء",
                "alt_en": "Product photo on a white background",
            }
        if "classification" in name or "sentiment" in name:
            return {"sentiment": "positive"}
        if "reply" in name:
            return {
                "suggested_reply_ar": "شكراً لملاحظاتك!",
                "suggested_reply_en": "Thank you for your feedback!",
            }
        if "insight" in name:
            return {
                "what_happened": "Reach grew this week, led by Instagram Reels.",
                "why": "Short-form video with clear CTAs outperformed static posts.",
                "what_to_change": "Replace one static offer with a Reel next week.",
                "next_actions": [
                    "Approve two Reel drafts",
                    "Post mid-week between 8–10 PM Riyadh",
                ],
            }
        if "brand" in name or "research" in name:
            return {
                "name": {
                    "value": "Example Clinic",
                    "confidence": 0.9,
                    "source_page_urls": ["https://example.sa/"],
                },
                "industry": {
                    "value": "healthcare",
                    "confidence": 0.75,
                    "source_page_urls": ["https://example.sa/"],
                },
                "description": {
                    "value": "Friendly modern dental care in Riyadh.",
                    "confidence": 0.8,
                    "source_page_urls": ["https://example.sa/"],
                },
                "colors": {
                    "value": ["#0B5D3B", "#134E4A"],
                    "confidence": 0.9,
                    "source_page_urls": ["https://example.sa/"],
                },
                "languages": {
                    "value": ["ar"],
                    "confidence": 0.85,
                    "source_page_urls": ["https://example.sa/"],
                },
                "logo_url": {
                    "value": "https://example.sa/logo.png",
                    "confidence": 0.7,
                    "source_page_urls": ["https://example.sa/"],
                },
                "warnings": [],
            }
        return {"result": "fake"}
