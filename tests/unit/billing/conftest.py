"""Unit billing tests need migrations applied (plans seed + tables)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic.config import Config

from alembic import command
from tests.db_fixtures import db_session  # noqa: F401


@pytest.fixture(scope="session", autouse=True)
def _apply_migrations() -> Iterator[None]:
    command.upgrade(Config("alembic.ini"), "head")
    yield
