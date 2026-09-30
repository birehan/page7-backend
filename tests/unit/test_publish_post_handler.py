"""Unit checks for the Phase 10 publish_post handler configuration."""

from __future__ import annotations

from app.features.posts.service import PUBLISH_POST_MAX_ATTEMPTS
from app.jobs.handlers import REGISTRY
from app.jobs.queue import timeout_for


def test_publish_post_max_attempts_bounded() -> None:
    assert PUBLISH_POST_MAX_ATTEMPTS == 10


def test_publish_post_handler_registered() -> None:
    assert "publish_post" in REGISTRY
    assert "reconcile_publications" in REGISTRY
    assert "sweep_publishing_invariants" in REGISTRY


def test_publish_post_timeout_configured() -> None:
    assert timeout_for("publish_post") == 45
