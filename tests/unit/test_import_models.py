"""Smoke: worker metadata includes review_links so posts FKs resolve."""

from __future__ import annotations

from sqlalchemy.orm import configure_mappers

from app.db import import_models as _import_models  # noqa: F401
from app.db.base import Base


def test_import_models_registers_review_links_for_posts_fk() -> None:
    configure_mappers()
    assert "review_links" in Base.metadata.tables
    assert "posts" in Base.metadata.tables
    posts = Base.metadata.tables["posts"]
    fk_targets = {fk.column.table.name for fk in posts.foreign_keys}
    assert "review_links" in fk_targets
