from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.features import organizations
from app.features.team import repository
from app.features.team.models import Membership


async def create_membership(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    user_id: uuid.UUID,
    role: str,
    invited_by: uuid.UUID | None = None,
) -> Membership:
    return await repository.create_membership(
        session,
        organization_id=organization_id,
        user_id=user_id,
        role=role,
        invited_by=invited_by,
    )


async def list_memberships(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> Sequence[Membership]:
    return await repository.list_memberships(session, organization_id=organization_id)


async def get_default_membership(session: AsyncSession, user_id: uuid.UUID) -> Membership | None:
    """A user's default active organization at login — see
    `repository.get_first_membership_for_user` for why "earliest joined" is
    the deterministic choice.
    """
    return await repository.get_first_membership_for_user(session, user_id)


async def get_membership(
    session: AsyncSession, *, organization_id: uuid.UUID, user_id: uuid.UUID
) -> Membership | None:
    return await repository.get_membership(
        session, organization_id=organization_id, user_id=user_id
    )


async def _assert_last_owner_protected(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    current_role: str,
    new_role: str | None,
) -> None:
    """architecture/05 §3's mutex, transcribed directly: lock the parent
    `organizations` row first (blocks a concurrent demote/remove/accept until
    this transaction commits), then count owners — both in the caller's own
    transaction, which is what makes the lock mean anything. `new_role=None`
    means removal. Only fires when the change actually moves the membership
    away from `'owner'` — a no-op edit or a promotion never needs the lock.
    """
    if current_role != "owner" or new_role == "owner":
        return
    await organizations.lock_organization_row(session, organization_id)
    owner_count = await repository.count_owners(session, organization_id=organization_id)
    if owner_count <= 1:
        raise ApiError("LAST_OWNER", "Cannot remove the organization's last owner", status_code=409)


async def remove_member(
    session: AsyncSession, *, organization_id: uuid.UUID, membership: Membership
) -> None:
    await _assert_last_owner_protected(
        session, organization_id=organization_id, current_role=membership.role, new_role=None
    )
    await repository.delete_membership(session, membership)


async def update_member_role(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    membership: Membership,
    role: str,
) -> Membership:
    await _assert_last_owner_protected(
        session, organization_id=organization_id, current_role=membership.role, new_role=role
    )
    membership.role = role
    await session.flush()
    return membership
