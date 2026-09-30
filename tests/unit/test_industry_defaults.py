"""Unit tests for industry-defaults lookup and pillar-diff logic."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.features.brands.industry_defaults import lookup_industry_defaults
from app.features.brands.schemas import ContentPillarIn
from app.features.brands.service import diff_pillars


def test_lookup_known_industries() -> None:
    for key in ("restaurant", "retail", "real_estate", "healthcare_clinic"):
        defaults = lookup_industry_defaults(key)
        assert len(defaults.pillars) == 3
        assert defaults.dialect in ("gulf", "msa")


def test_lookup_normalizes_and_falls_back() -> None:
    assert lookup_industry_defaults("  Restaurant ").pillars[0]["name"] == "Menu highlights"
    fallback = lookup_industry_defaults("unknown-widget-factory")
    assert [p["name"] for p in fallback.pillars] == [
        "What we offer",
        "Behind the scenes",
        "Customer love",
    ]


def test_lookup_alias_keywords() -> None:
    assert lookup_industry_defaults("Healthcare — Dental").pillars[0]["name"] == "Services offered"
    assert lookup_industry_defaults("مستشفى تخصصي").pillars[0]["name"] == "Services offered"
    assert lookup_industry_defaults("Cafe & bakery").pillars[0]["name"] == "Menu highlights"


def _pillar(name: str, *, pillar_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=pillar_id or uuid.uuid4(),
        name=name,
        description="",
        weight=1,
        position=0,
    )


def test_diff_pillars_add_update_delete_reposition() -> None:
    keep_id = uuid.uuid4()
    drop_id = uuid.uuid4()
    existing = [_pillar("Keep", pillar_id=keep_id), _pillar("Drop", pillar_id=drop_id)]
    new_id = uuid.uuid4()
    incoming = [
        ContentPillarIn(id=new_id, name="New", description="n", weight=2),
        ContentPillarIn(id=keep_id, name="Keep renamed", description="k", weight=3),
    ]
    to_insert, to_update, to_delete = diff_pillars(existing, incoming)  # type: ignore[arg-type]
    assert [p.id for p in to_insert] == [new_id]
    assert len(to_update) == 1
    assert to_update[0][0].id == keep_id
    assert to_update[0][2] == 1  # position from array order
    assert to_delete == [drop_id]
