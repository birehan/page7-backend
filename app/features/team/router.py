from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.request_context import AccessContext, require_membership, require_min_role
from app.db.session import get_db_session
from app.features import auth
from app.features.team import service as team
from app.features.team.models import Membership
from app.features.team.schemas import (
    InviteMemberBody,
    ResendInviteBody,
    TeamMemberOut,
    UpdateMemberRoleBody,
)

if TYPE_CHECKING:
    # Type-only, per the import-linter feature-boundary contract — every
    # runtime Invitation instance here comes back from an `auth.*` barrel
    # call, never a direct import of `app.features.auth.models`.
    from app.features.auth.models import Invitation

router = APIRouter(prefix="/orgs/{orgId}/team", tags=["team"])


def _member_to_out(
    membership: Membership, *, name: str, email: str, avatar_url: str | None
) -> TeamMemberOut:
    return TeamMemberOut(
        id=membership.user_id,
        name=name,
        email=email,
        role=membership.role,
        status="active",
        avatar_url=avatar_url,
    )


def _invitation_to_out(invitation: Invitation) -> TeamMemberOut:
    return TeamMemberOut(
        id=invitation.id,
        name=invitation.email.split("@")[0],
        email=invitation.email,
        role=invitation.role,
        status="pending",
    )


@router.get("", response_model=list[TeamMemberOut])
async def list_team(
    ctx: Annotated[AccessContext, Depends(require_membership)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> list[TeamMemberOut]:
    memberships = await team.list_memberships(db, organization_id=ctx.organization_id)
    users = await auth.get_users_by_ids(db, [m.user_id for m in memberships])
    invitations = await auth.list_pending_invitations(db, organization_id=ctx.organization_id)

    active = [
        _member_to_out(
            m,
            name=users[m.user_id].name if m.user_id in users else "",
            email=users[m.user_id].email if m.user_id in users else "",
            avatar_url=users[m.user_id].avatar_url if m.user_id in users else None,
        )
        for m in memberships
    ]
    pending = [_invitation_to_out(inv) for inv in invitations]
    return active + pending


@router.post("", response_model=TeamMemberOut, status_code=status.HTTP_201_CREATED)
async def invite_member(
    body: InviteMemberBody,
    ctx: Annotated[AccessContext, Depends(require_min_role("admin"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> TeamMemberOut:
    org = await auth.get_session_organization(db, organization_id=ctx.organization_id)
    invitation = await auth.create_invitation(
        db,
        organization_id=ctx.organization_id,
        organization_name=org.name,
        email=body.email,
        role=body.role,
        invited_by_user_id=ctx.user_id,
        invited_by_name=ctx.user_name,
        locale=body.locale,
    )
    return _invitation_to_out(invitation)


@router.patch("/{memberId}", response_model=TeamMemberOut)
async def update_member_role(
    memberId: str,  # noqa: N803 - matches the URL's camelCase path param
    body: UpdateMemberRoleBody,
    ctx: Annotated[AccessContext, Depends(require_min_role("admin"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> TeamMemberOut:
    membership = await team.get_membership(
        db, organization_id=ctx.organization_id, user_id=uuid.UUID(memberId)
    )
    if membership is None:
        raise ApiError("NOT_FOUND", "Team member not found", status_code=404)
    updated = await team.update_member_role(
        db, organization_id=ctx.organization_id, membership=membership, role=body.role
    )
    users = await auth.get_users_by_ids(db, [updated.user_id])
    info = users.get(updated.user_id)
    return _member_to_out(
        updated,
        name=info.name if info else "",
        email=info.email if info else "",
        avatar_url=info.avatar_url if info else None,
    )


@router.delete("/{memberId}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    memberId: str,  # noqa: N803 - matches the URL's camelCase path param
    ctx: Annotated[AccessContext, Depends(require_min_role("admin"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
) -> None:
    member_uuid = uuid.UUID(memberId)
    # The contract's TeamMember.id merges two id spaces (architecture/02 §1's
    # "Contract smells" #7) — resolve against an active membership first,
    # then a pending invitation.
    membership = await team.get_membership(
        db, organization_id=ctx.organization_id, user_id=member_uuid
    )
    if membership is not None:
        await team.remove_member(db, organization_id=ctx.organization_id, membership=membership)
        return

    invitation = await auth.get_invitation_by_id(
        db, organization_id=ctx.organization_id, invitation_id=member_uuid
    )
    if invitation is not None:
        await auth.revoke_invitation(db, invitation=invitation)
        return

    raise ApiError("NOT_FOUND", "Team member not found", status_code=404)


@router.post("/{memberId}/resend-invite", response_model=TeamMemberOut)
async def resend_invite(
    memberId: str,  # noqa: N803 - matches the URL's camelCase path param
    ctx: Annotated[AccessContext, Depends(require_min_role("admin"))],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    body: ResendInviteBody | None = None,
) -> TeamMemberOut:
    invitation = await auth.get_invitation_by_id(
        db, organization_id=ctx.organization_id, invitation_id=uuid.UUID(memberId)
    )
    if invitation is None:
        raise ApiError("NOT_FOUND", "Invitation not found", status_code=404)
    org = await auth.get_session_organization(db, organization_id=ctx.organization_id)
    locale = body.locale if body is not None else "ar"
    updated = await auth.resend_invitation(
        db,
        invitation=invitation,
        organization_name=org.name,
        inviter_name=ctx.user_name,
        locale=locale,
    )
    return _invitation_to_out(updated)
