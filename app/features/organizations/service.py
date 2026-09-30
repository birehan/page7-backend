from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.features import audit
from app.features.organizations import repository
from app.features.organizations.models import Organization, OrgSettings
from app.features.organizations.schemas import UpdateOrganizationBody, UpdateOrgSettingsBody


async def create_organization_for_signup(
    session: AsyncSession, *, name: str, created_by_user_id: uuid.UUID
) -> Organization:
    """Called by `features.auth.service.signup()` (M7) inside its own
    transaction — an organization is never created without its companion
    `org_settings` row existing in the same beat (architecture/02 §2:
    `org_settings` is "created alongside" its organization).
    """
    org = await repository.create_organization(session, name=name, created_by=created_by_user_id)
    await repository.create_org_settings(session, org.id)
    from app.features import billing as billing_feature

    await billing_feature.create_default_subscription(
        session, organization_id=org.id
    )
    return org


async def get_organization(session: AsyncSession, organization_id: uuid.UUID) -> Organization:
    return await repository.get_organization(session, organization_id)


async def update_organization(
    session: AsyncSession, organization_id: uuid.UUID, body: UpdateOrganizationBody
) -> Organization:
    fields = body.model_dump(exclude_unset=True)
    if fields.get("vat_number") == "":
        # The frontend form allows clearing the field to an empty string;
        # the DB's CHECK constraint accepts only a 15-digit string or NULL.
        fields["vat_number"] = None
    return await repository.update_organization(session, organization_id, **fields)


async def get_org_settings(session: AsyncSession, organization_id: uuid.UUID) -> OrgSettings:
    return await repository.get_org_settings(session, organization_id)


async def update_org_settings(
    session: AsyncSession, organization_id: uuid.UUID, body: UpdateOrgSettingsBody
) -> OrgSettings:
    fields = body.model_dump(exclude_unset=True)
    if "approval_policy" in fields:
        policy = fields.pop("approval_policy")
        fields["approval_mode"] = policy["mode"]
        fields["risk_threshold"] = policy["risk_threshold"]
    return await repository.update_org_settings(session, organization_id, **fields)


async def freeze_publishing(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_name: str,
    reason: str | None,
) -> OrgSettings:
    settings = await repository.update_org_settings(
        session,
        organization_id,
        publishing_frozen=True,
        frozen_by_user_id=actor_user_id,
        frozen_by_name=actor_name,
        frozen_at=utc_now(),
        frozen_reason=reason,
    )
    await audit.record(
        session,
        organization_id=organization_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        action="publishing.frozen",
        target_type="organization",
        target_id=organization_id,
        meta={"reason": reason} if reason else None,
    )
    return settings


async def unfreeze_publishing(
    session: AsyncSession, *, organization_id: uuid.UUID, actor_user_id: uuid.UUID, actor_name: str
) -> OrgSettings:
    settings = await repository.update_org_settings(
        session,
        organization_id,
        publishing_frozen=False,
        frozen_by_user_id=None,
        frozen_by_name=None,
        frozen_at=None,
        frozen_reason=None,
    )
    await audit.record(
        session,
        organization_id=organization_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        action="publishing.unfrozen",
        target_type="organization",
        target_id=organization_id,
    )
    return settings


async def delete_organization(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_name: str,
) -> None:
    """Owner-only soft-delete + schedule PDPL purge (Phase 15).

    Sets `deleted_at` and enqueues `maintenance.purge_organization` 30 days out
    in the same transaction. Idempotent: a second call against an already
    pending deletion returns without resetting the purge clock.
    """
    from datetime import timedelta

    from app.jobs import queue as job_queue

    org = await repository.get_organization(session, organization_id)
    if org.deleted_at is not None:
        return

    now = utc_now()
    org.deleted_at = now
    org.deleted_by = actor_user_id
    await session.flush()

    await job_queue.enqueue(
        session,
        queue="maintenance",
        type="maintenance.purge_organization",
        payload={"organization_id": str(organization_id)},
        unique_key=f"purge_org:{organization_id}",
        run_at=now + timedelta(days=30),
        organization_id=organization_id,
    )
    await audit.record(
        session,
        organization_id=organization_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        action="org.deleted",
        target_type="organization",
        target_id=organization_id,
        meta={"purge_in_days": 30},
    )
