from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.auth.models import Session as AuthSession
from app.features.auth.models import User
from app.features.team.models import Membership
from app.main import create_app
from tests.db_fixtures import SeedMember


def _client_for(raw_token: str) -> AsyncClient:
    settings = get_settings()
    transport = ASGITransport(app=create_app())
    return AsyncClient(
        transport=transport,
        base_url="https://test",
        headers={"Origin": settings.cors.allowed_origins[0]},
        cookies={settings.auth.session_cookie_name: raw_token},
    )


async def test_two_concurrent_demotions_of_the_organizations_two_owners_leave_exactly_one(
    db_session: AsyncSession, seed_member: SeedMember
) -> None:
    """architecture/05 §3: an org with exactly two owners, and two concurrent
    requests each demoting a *different* one of the two at the same moment.
    Must use `asyncio.gather`, not sequential awaits — each request needs the
    `organizations` row lock to be contended genuinely concurrently, or this
    test cannot exercise the race at all; a request-scoped session (from
    `get_db_session`) gets its own connection, so two concurrent requests are
    two genuinely separate Postgres transactions.
    """
    org_id, owner_a, token_a = await seed_member(role="owner")

    # A second owner in the SAME org, seeded directly (seed_member always
    # creates a fresh org, so the second owner is added here by hand).
    user_b = User(email=f"{uuid.uuid4()}@example.com", name="Second Owner")
    db_session.add(user_b)
    await db_session.flush()
    db_session.add(Membership(organization_id=org_id, user_id=user_b.id, role="owner"))
    raw_token_b = uuid.uuid4().hex
    db_session.add(
        AuthSession(
            user_id=user_b.id,
            organization_id=org_id,
            token_hash=hashlib.sha256(raw_token_b.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(hours=12),
        )
    )
    await db_session.commit()

    async def _demote(raw_token: str, target_user_id: uuid.UUID) -> int:
        async with _client_for(raw_token) as own_client:
            response = await own_client.patch(
                f"/v1/orgs/{org_id}/team/{target_user_id}", json={"role": "admin"}
            )
            return response.status_code

    # Owner A demotes owner B; owner B demotes owner A — genuinely
    # concurrent, so the last-owner mutex is what decides which one wins.
    status_a, status_b = await asyncio.gather(
        _demote(token_a, user_b.id), _demote(raw_token_b, owner_a)
    )

    statuses = sorted([status_a, status_b])
    assert statuses == [200, 409], (status_a, status_b)

    remaining_owners = (
        (
            await db_session.execute(
                select(Membership).where(
                    Membership.organization_id == org_id, Membership.role == "owner"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(remaining_owners) == 1
