from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.team.models import Membership


async def get_membership(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> Membership | None:
    stmt = select(Membership).where(
        Membership.organization_id == organization_id, Membership.user_id == user_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_first_membership_for_user(
    session: AsyncSession, user_id: uuid.UUID
) -> Membership | None:
    """Picks a user's default active organization at login — the schema
    allows multiple memberships per user, but Phase 2 has no concept of a
    remembered "last active org" for a fresh login to prefer, so the
    earliest-joined membership is the deterministic default.
    """
    stmt = (
        select(Membership)
        .where(Membership.user_id == user_id)
        .order_by(Membership.joined_at)
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_memberships(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> Sequence[Membership]:
    stmt = select(Membership).where(Membership.organization_id == organization_id)
    return (await session.execute(stmt)).scalars().all()


async def create_membership(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    invited_by: uuid.UUID | None = None,
) -> Membership:
    membership = Membership(
        organization_id=organization_id, user_id=user_id, role=role, invited_by=invited_by
    )
    session.add(membership)
    await session.flush()
    return membership


async def count_owners(session: AsyncSession, *, organization_id: uuid.UUID) -> int:
    stmt = (
        select(func.count())
        .select_from(Membership)
        .where(Membership.organization_id == organization_id, Membership.role == "owner")
    )
    return (await session.execute(stmt)).scalar_one()


async def delete_membership(session: AsyncSession, membership: Membership) -> None:
    await session.delete(membership)
    await session.flush()
