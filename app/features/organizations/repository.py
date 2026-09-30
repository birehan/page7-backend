from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.organizations.models import Organization, OrgSettings


async def lock_organization_row(session: AsyncSession, organization_id: uuid.UUID) -> None:
    """The last-owner-protection mutex (architecture/05 §3): `SELECT ... FOR
    UPDATE` on the parent row, taken by `features/team` before it counts and
    mutates owner memberships — locking `memberships` rows directly wouldn't
    cover a phantom new owner from a concurrent invitation-accept. Exported
    through this barrel rather than `features/team` reaching `organizations`
    with its own raw SQL, so a table still has exactly one owning module even
    for a side effect that isn't a normal CRUD operation.
    """
    await session.execute(
        text("SELECT id FROM organizations WHERE id = :organization_id FOR UPDATE"),
        {"organization_id": organization_id},
    )


async def get_organization(session: AsyncSession, organization_id: uuid.UUID) -> Organization:
    org = await session.get(Organization, organization_id)
    if org is None:
        raise LookupError(f"organization {organization_id} not found")
    return org


async def create_organization(
    session: AsyncSession, *, name: str, created_by: uuid.UUID
) -> Organization:
    org = Organization(name=name, created_by=created_by)
    session.add(org)
    await session.flush()
    return org


async def update_organization(
    session: AsyncSession, organization_id: uuid.UUID, **fields: Any
) -> Organization:
    org = await get_organization(session, organization_id)
    for key, value in fields.items():
        setattr(org, key, value)
    await session.flush()
    return org


async def get_org_settings(session: AsyncSession, organization_id: uuid.UUID) -> OrgSettings:
    settings = await session.get(OrgSettings, organization_id)
    if settings is None:
        raise LookupError(f"org_settings for organization {organization_id} not found")
    return settings


async def create_org_settings(session: AsyncSession, organization_id: uuid.UUID) -> OrgSettings:
    settings = OrgSettings(organization_id=organization_id)
    session.add(settings)
    await session.flush()
    return settings


async def update_org_settings(
    session: AsyncSession, organization_id: uuid.UUID, **fields: Any
) -> OrgSettings:
    settings = await get_org_settings(session, organization_id)
    for key, value in fields.items():
        setattr(settings, key, value)
    await session.flush()
    return settings
