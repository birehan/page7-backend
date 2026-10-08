from __future__ import annotations

import uuid
from typing import Literal

from pydantic import EmailStr

from app.core.schema import CamelModel

Role = Literal["owner", "admin", "editor", "approver", "viewer"]


class TeamMemberOut(CamelModel):
    """The contract's `TeamMember.id` merges two id spaces: `users.id` for an
    active member, `invitations.id` for a pending one (architecture/02 §1's
    "Contract smells" #7) — a single, resolvable lookup path, not two
    endpoints the frontend would have to choose between.
    """

    id: uuid.UUID
    name: str
    email: EmailStr
    role: Role
    status: Literal["active", "pending"]
    avatar_url: str | None = None


class InviteMemberBody(CamelModel):
    email: EmailStr
    role: Role
    # Active UI language of the inviter — drives invite email copy + accept link.
    locale: Literal["ar", "en"] = "ar"


class UpdateMemberRoleBody(CamelModel):
    role: Role


class ResendInviteBody(CamelModel):
    locale: Literal["ar", "en"] = "ar"
