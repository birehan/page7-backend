from __future__ import annotations

import uuid
from typing import Literal

from pydantic import EmailStr, Field

from app.core.schema import CamelModel

Role = Literal["owner", "admin", "editor", "approver", "viewer"]


class SessionUserOut(CamelModel):
    id: uuid.UUID
    name: str
    name_ar: str
    email: EmailStr
    role: Role
    avatar_url: str | None = None


class SessionPayloadOut(CamelModel):
    user: SessionUserOut
    organization_id: uuid.UUID
    organization_name: str
    organization_industry: str
    organization_city: str
    organization_vat_number: str
    # `brands` doesn't exist until Phase 4 — omitted, never a placeholder id
    # (auth.contract.ts's `brandId` was loosened to `.optional()` for this).
    brand_id: uuid.UUID | None = None
    expires_at: int
    mfa_enabled: bool


class AuthenticatedLoginOut(CamelModel):
    status: Literal["authenticated"] = "authenticated"
    session: SessionPayloadOut


class MfaRequiredLoginOut(CamelModel):
    status: Literal["mfa_required"] = "mfa_required"
    challenge_token: str


class EmailVerificationRequiredOut(CamelModel):
    status: Literal["email_verification_required"] = "email_verification_required"
    challenge_token: str
    email: str


LoginResponseOut = AuthenticatedLoginOut | MfaRequiredLoginOut | EmailVerificationRequiredOut
SignupResponseOut = EmailVerificationRequiredOut


class LoginBody(CamelModel):
    email: EmailStr
    password: str = Field(min_length=1)
    remember_me: bool = False


class SignupBody(CamelModel):
    name: str = Field(min_length=1)
    email: EmailStr
    password: str = Field(min_length=8)
    organization_name: str = Field(min_length=1)
    locale: Literal["ar", "en"] = "en"


class VerifyEmailBody(CamelModel):
    challenge_token: str = Field(min_length=1)
    code: str = Field(pattern=r"^\d{6}$")


class ResendVerificationBody(CamelModel):
    challenge_token: str = Field(min_length=1)


class ForgotPasswordBody(CamelModel):
    email: EmailStr


class ResetPasswordBody(CamelModel):
    token: str = Field(min_length=1)
    password: str = Field(min_length=8)


class InvitePreviewOut(CamelModel):
    organization_name: str
    email: EmailStr
    role: Role
    inviter_name: str
    expires_at: str


class AcceptInviteBody(CamelModel):
    name: str = Field(min_length=1)
    password: str = Field(min_length=8)
    locale: Literal["ar", "en"] = "en"


class MfaEnrollResponseOut(CamelModel):
    secret: str
    otpauth_url: str
    recovery_codes: list[str]


class MfaVerifyBody(CamelModel):
    code: str = Field(pattern=r"^\d{6}$")
    challenge_token: str | None = None


class UserOut(CamelModel):
    id: uuid.UUID
    name: str
    name_ar: str | None = None
    email: EmailStr
    avatar_url: str | None = None


class UpdateUserBody(CamelModel):
    name: str | None = None
    name_ar: str | None = None
    avatar_url: str | None = None


class AuthProvidersOut(CamelModel):
    google: bool


class GoogleStartOut(CamelModel):
    authorize_url: str


class SessionHandoffBody(CamelModel):
    handoff: str = Field(min_length=1)
