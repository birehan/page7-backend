"""Shared DB-backed fixtures for any test directory that needs real Postgres
rows — imported into each directory's own `conftest.py` (`from
tests.db_fixtures import db_session, ...`) rather than centralized in the
top-level `tests/conftest.py`, so `tests/unit/` stays conceptually
DB-independent, matching this suite's existing per-directory `_apply_migrations`
pattern.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session_factory
from app.features.auth.models import Session as AuthSession
from app.features.auth.models import User
from app.features.organizations.models import Organization, OrgSettings
from app.features.team.models import Membership

SeedMember = Callable[..., Awaitable[tuple[uuid.UUID, uuid.UUID, str]]]


@pytest.fixture
async def db_session() -> AsyncIterator[AsyncSession]:
    """A raw session for tests that need to seed rows directly, below the API
    layer — e.g. a session cookie no signup endpoint exists yet to mint.
    """
    async with get_session_factory()() as session:
        yield session
        await session.rollback()


@pytest.fixture
def seed_member(db_session: AsyncSession) -> SeedMember:
    """Creates a user, an org, a membership at the given role, and a live
    session; returns `(organization_id, user_id, raw_cookie_value)`. Commits —
    a request against the real app resolves its own session from a different
    connection in the pool, which only sees committed rows.
    """

    async def _seed(*, role: str) -> tuple[uuid.UUID, uuid.UUID, str]:
        user = User(email=f"{uuid.uuid4()}@example.com", name="Seeded User")
        org = Organization(name="Seeded Org")
        db_session.add_all([user, org])
        await db_session.flush()

        # Every real org gets a companion row in the same beat
        # (create_organization_for_signup) — seeded here too so a test
        # exercising org_settings doesn't need its own separate setup.
        db_session.add(OrgSettings(organization_id=org.id))
        db_session.add(Membership(organization_id=org.id, user_id=user.id, role=role))

        from app.features.billing.repository import create_default_subscription

        await create_default_subscription(db_session, organization_id=org.id)

        raw_token = uuid.uuid4().hex
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        db_session.add(
            AuthSession(
                user_id=user.id,
                organization_id=org.id,
                token_hash=token_hash,
                expires_at=datetime.now(UTC) + timedelta(hours=12),
            )
        )
        await db_session.commit()
        return org.id, user.id, raw_token

    return _seed
