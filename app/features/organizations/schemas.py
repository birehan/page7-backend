from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import field_validator

from app.core.schema import CamelModel

_VAT_NUMBER_RE = re.compile(r"\d{15}")


class OrganizationOut(CamelModel):
    id: uuid.UUID
    name: str
    industry: str
    city: str
    vat_number: str | None = None
    created_at: datetime


class UpdateOrganizationBody(CamelModel):
    name: str | None = None
    industry: str | None = None
    city: str | None = None
    vat_number: str | None = None

    @field_validator("vat_number")
    @classmethod
    def _validate_vat_number(cls, value: str | None) -> str | None:
        """Matches `organizations`' own CHECK constraint (architecture/02 §2)
        — validated here so a malformed value is a `422 VALIDATION` at the API
        boundary, never a raw DB constraint violation leaking out as a `500`.
        An empty string is allowed through (normalized to `NULL` in
        `service.update_organization`, matching the "clear the field" form
        interaction) even though the DB constraint itself would reject it.
        """
        if value is None or value == "" or _VAT_NUMBER_RE.fullmatch(value):
            return value
        raise ValueError("vat_number must be exactly 15 digits, or empty")


class ApprovalPolicy(CamelModel):
    mode: Literal["always", "autonomous"]
    risk_threshold: float


class OnboardingProgress(CamelModel):
    brand: bool
    channel: bool
    plan: bool
    first_approval: bool


class BusinessProfile(CamelModel):
    languages: str
    audience: str
    goal: str


class OrgSettingsOut(CamelModel):
    id: uuid.UUID
    publishing_frozen: bool
    frozen_by: str | None = None
    frozen_at: datetime | None = None
    frozen_reason: str | None = None
    approval_policy: ApprovalPolicy
    notification_prefs: dict[str, bool]
    onboarding: OnboardingProgress
    business_profile: BusinessProfile | None = None


class UpdateOrgSettingsBody(CamelModel):
    """Deliberately narrower than the frontend's mock-mode contract (a bare
    `orgSettingsSchema.partial()`, which would technically also accept
    `publishingFrozen`/`frozenBy`/`frozenAt`): freeze state changes only
    through `/settings/freeze` and `/settings/unfreeze`, never a generic
    PATCH, so those fields aren't writable here at all — `extra="forbid"`
    (via `CamelModel`) rejects a request that tries.
    """

    approval_policy: ApprovalPolicy | None = None
    notification_prefs: dict[str, bool] | None = None
    onboarding: OnboardingProgress | None = None
    business_profile: BusinessProfile | None = None


class FreezePublishingBody(CamelModel):
    reason: str | None = None
