"""Unit tests for Phase 7 content AI prompts and schema constraints."""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.core.ids import new_uuid7
from app.features.content_ai.prompts import (
    alt_text,
    caption_generation,
    caption_transform,
    plan_generation,
    strategy_generation,
)
from app.features.content_ai.prompts.corpus import load_caption_corpus
from app.features.content_ai.prompts.schema_utils import assert_anthropic_compatible
from app.features.strategy.schemas import StrategyGoalOut, StrategyOut


def test_caption_corpus_has_24_themes() -> None:
    corpus = load_caption_corpus()
    assert len(corpus) == 24
    assert corpus[0]["theme_index"] == 0
    assert "{brand}" in str(corpus[0]["ar_gulf"])
    assert "{brand}" in str(corpus[0]["en"])


def test_all_output_schemas_are_anthropic_compatible() -> None:
    for schema in (
        caption_generation.OUTPUT_SCHEMA,
        caption_transform.OUTPUT_SCHEMA,
        plan_generation.OUTPUT_SCHEMA,
        strategy_generation.OUTPUT_SCHEMA,
        alt_text.OUTPUT_SCHEMA,
    ):
        assert_anthropic_compatible(schema)


def test_openai_strict_requires_every_property_key() -> None:
    """OpenAI strict JSON schema rejects objects whose `required` omits a property."""
    import pytest

    from app.features.content_ai.prompts.schema_utils import assert_anthropic_compatible

    broken = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"title": {"type": "string"}, "platform": {"type": "string"}},
        "required": ["platform"],
    }
    with pytest.raises(ValueError, match="missing \\['title'\\]"):
        assert_anthropic_compatible(broken)


def test_caption_generation_build_includes_brand() -> None:
    messages = caption_generation.build(
        brand={"name": "Cafe Riyadh", "industry": "retail", "city": "Riyadh", "guidelines": {}},
        pillar={"name": "Tips", "description": "daily tips"},
        cultural_event=None,
        platforms=["instagram"],
        dialect="gulf",
        count=3,
    )
    assert messages[0].role == "system"
    joined = " ".join(str(m.content) for m in messages)
    assert "Cafe Riyadh" in joined


def test_caption_transform_build_includes_seed() -> None:
    messages = caption_transform.build(
        brand={"name": "Cafe Riyadh", "guidelines": {}},
        pillar=None,
        cultural_event=None,
        platforms=["instagram"],
        dialect="msa",
        intent="shorter",
        count=1,
        seed={"ar": "نص طويل", "en": "long text", "hashtags": ["#a"]},
    )
    joined = " ".join(str(m.content) for m in messages)
    assert "shorter" in joined.lower() or "نص طويل" in joined


def test_plan_and_strategy_build() -> None:
    plan_msgs = plan_generation.build(
        brand={"name": "Brand", "city": "Riyadh", "pillars": [], "guidelines": {}},
        from_date=date(2026, 9, 1),
        to_date=date(2026, 9, 14),
        cadence={"instagram": 3},
        only_gaps=True,
        cultural_events=[],
        existing_summary="none",
        target_count=14,
    )
    assert any(m.role == "user" for m in plan_msgs)
    joined = " ".join(str(m.content) for m in plan_msgs)
    assert "exactly 14" in joined
    assert "inclusive" in joined.lower()
    assert "day_offset" in joined

    strategy = StrategyOut(
        id=new_uuid7(),
        goals=[StrategyGoalOut(id=new_uuid7(), text="Grow", progress=0.1)],
        cadence={"instagram": 3.0},
        updated_at=datetime.now(UTC),
    )
    strategy_msgs = strategy_generation.build(
        brand={"name": "Brand", "industry": "retail", "pillars": [], "guidelines": {}},
        current=strategy,
    )
    assert any("Grow" in str(m.content) for m in strategy_msgs)


def test_alt_text_build() -> None:
    messages = alt_text.build(caption="a latte on a table")
    assert messages[0].role == "system"
    assert "latte" in str(messages[-1].content)


def test_plan_generation_scheduled_time_rejects_a_full_datetime_string() -> None:
    # Regression: a cheap/less-compliant model once emitted
    # "2026-09-28T2026-09-28T10:00:00:00" for scheduled_time (a raw echo of the
    # date_key concatenated with itself), which riyadh_datetime_to_utc()
    # (app/core/time.py) cannot parse — a real 500 in stream_plan, not caused
    # by the frontend. The strict output schema itself must reject anything
    # that isn't bare "HH:MM" so a non-compliant model's raw output never
    # reaches the parser at all.
    import re

    schema = plan_generation.OUTPUT_SCHEMA
    time_schema = schema["properties"]["items"]["items"]["properties"]["scheduled_time"]
    pattern = re.compile(time_schema["pattern"])
    assert pattern.fullmatch("14:00")
    assert pattern.fullmatch("00:00")
    assert pattern.fullmatch("23:59")
    assert not pattern.fullmatch("2026-09-28T2026-09-28T10:00:00:00")
    assert not pattern.fullmatch("10:00:00")
    assert not pattern.fullmatch("2026-09-28T10:00")
