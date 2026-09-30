from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.request_context import AccessContext, require_capability, require_min_role
from app.db.session import get_db_session
from app.features.organizations import service
from app.features.organizations.models import Organization, OrgSettings
from app.features.organizations.schemas import (
    ApprovalPolicy,
    BusinessProfile,
    FreezePublishingBody,
    OnboardingProgress,
    OrganizationOut,
    OrgSettingsOut,
    UpdateOrganizationBody,
    UpdateOrgSettingsBody,
)

router = APIRouter(prefix="/orgs/{orgId}", tags=["organizations"])


def _to_organization_out(org: Organization) -> OrganizationOut:
    return OrganizationOut(
        id=org.id,
        name=org.name,
        industry=org.industry,
        city=org.city,
        vat_number=org.vat_number,
        created_at=org.created_at,
    )


def _to_org_settings_out(settings: OrgSettings) -> OrgSettingsOut:
    return OrgSettingsOut(
        id=settings.organization_id,
        publishing_frozen=settings.publishing_frozen,
        frozen_by=settings.frozen_by_name,
        frozen_at=settings.frozen_at,
        frozen_reason=settings.frozen_reason,
        approval_policy=ApprovalPolicy(
            mode=settings.approval_mode, risk_threshold=float(settings.risk_threshold)
        ),
        notification_prefs=settings.notification_prefs,
        onboarding=OnboardingProgress(**settings.onboarding),
        business_profile=(
            BusinessProfile(**settings.business_profile) if settings.business_profile else None
        ),
    )


@router.get("", response_model=OrganizationOut, response_model_exclude_none=True)
async def get_organization(
    ctx: Annotated[AccessContext, Depends(require_min_role("admin"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> OrganizationOut:
    org = await service.get_organization(db, ctx.organization_id)
    return _to_organization_out(org)


@router.patch("", response_model=OrganizationOut, response_model_exclude_none=True)
async def update_organization(
    body: UpdateOrganizationBody,
    ctx: Annotated[AccessContext, Depends(require_min_role("admin"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> OrganizationOut:
    org = await service.update_organization(db, ctx.organization_id, body)
    return _to_organization_out(org)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT)
async def delete_organization(
    ctx: Annotated[AccessContext, Depends(require_capability("org.delete"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> Response:
    """Soft-delete the organization and enqueue a 30-day PDPL purge.

    Idempotent: a second call against an already-pending deletion returns 204
    without resetting the purge clock (Phase 15).
    """
    await service.delete_organization(
        db,
        organization_id=ctx.organization_id,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/settings", response_model=OrgSettingsOut, response_model_exclude_none=True)
async def get_org_settings(
    ctx: Annotated[AccessContext, Depends(require_min_role("admin"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> OrgSettingsOut:
    settings = await service.get_org_settings(db, ctx.organization_id)
    return _to_org_settings_out(settings)


@router.patch("/settings", response_model=OrgSettingsOut, response_model_exclude_none=True)
async def update_org_settings(
    body: UpdateOrgSettingsBody,
    ctx: Annotated[AccessContext, Depends(require_min_role("admin"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> OrgSettingsOut:
    settings = await service.update_org_settings(db, ctx.organization_id, body)
    return _to_org_settings_out(settings)


@router.post("/settings/freeze", response_model=OrgSettingsOut, response_model_exclude_none=True)
async def freeze_publishing(
    body: FreezePublishingBody,
    ctx: Annotated[AccessContext, Depends(require_capability("publishing.freeze"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> OrgSettingsOut:
    settings = await service.freeze_publishing(
        db,
        organization_id=ctx.organization_id,
        actor_user_id=ctx.user_id,
        actor_name=ctx.user_name,
        reason=body.reason,
    )
    return _to_org_settings_out(settings)


@router.post("/settings/unfreeze", response_model=OrgSettingsOut, response_model_exclude_none=True)
async def unfreeze_publishing(
    ctx: Annotated[AccessContext, Depends(require_capability("publishing.freeze"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> OrgSettingsOut:
    settings = await service.unfreeze_publishing(
        db, organization_id=ctx.organization_id, actor_user_id=ctx.user_id, actor_name=ctx.user_name
    )
    return _to_org_settings_out(settings)
