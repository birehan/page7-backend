from __future__ import annotations

from collections.abc import Callable, Coroutine, Iterator
from typing import Any

import pytest
from alembic.config import Config
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.integrations.email.fakes import FakeEmailProvider
from tests.db_fixtures import db_session, seed_member  # noqa: F401
from tests.email_fixtures import client_with_fake_email  # noqa: F401
from tests.job_test_helpers import drain_email_jobs


@pytest.fixture(scope="session", autouse=True)
def _apply_migrations() -> Iterator[None]:
    """Runs once per test session against DATABASE__URL — real Postgres (the
    compose service on localhost:5544 locally, or CI's service container), per
    architecture/14, never a mock.
    """
    command.upgrade(Config("alembic.ini"), "head")
    yield


@pytest.fixture
def drain_email(  # noqa: F811
    db_session: AsyncSession,  # noqa: F811
    client_with_fake_email: tuple[AsyncClient, FakeEmailProvider],  # noqa: F811
) -> Callable[[], Coroutine[Any, Any, None]]:
    """Return a `drain()` coroutine that runs all pending email.send jobs.

    Usage::

        async def test_something(client_with_fake_email, drain_email):
            client, fake = client_with_fake_email
            await client.post("/v1/auth/forgot", json={"email": "...@..."})
            await drain_email()
            assert fake.sent[0].to == "..."
    """
    _, fake = client_with_fake_email

    async def _drain() -> None:
        await drain_email_jobs(db_session, fake)

    return _drain
