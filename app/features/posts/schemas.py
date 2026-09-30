from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.core.schema import CamelModel

Platform = Literal["instagram", "facebook", "tiktok", "snapchat", "whatsapp"]
PostStatus = Literal[
    "idea",
    "drafting",
    "draft",
    "in_review",
    "changes_requested",
    "approved",
    "scheduled",
    "publishing",
    "published",
    "failed",
]
Dialect = Literal["gulf", "msa"]
Lang = Literal["ar", "en"]
RiskSeverity = Literal["low", "medium", "high"]
VersionReason = Literal[
    "created", "edited", "ai_generated", "changes_applied", "restored"
]
RiskReasonCode = Literal[
    "banned_claim",
    "caption_over_limit",
    "too_many_hashtags",
    "missing_cta",
    "no_alt_text",
    "prayer_time_overlap",
    "cultural_sensitivity",
    "missing_ad_disclosure",
]


class PostVariantIn(CamelModel):
    lang: Lang
    dialect: Dialect | None = None
    caption: str
    hashtags: list[str]


class PostMediaIn(CamelModel):
    id: str
    kind: Literal["image", "video"]
    url: str
    alt: str
    library_id: str | None = None


class DisclosureIn(CamelModel):
    is_paid: bool


class RiskReasonOut(CamelModel):
    code: RiskReasonCode
    severity: RiskSeverity
    params: dict[str, str | int] | None = None


class RiskReportOut(CamelModel):
    score: float = Field(ge=0, le=1)
    reasons: list[RiskReasonOut]


class PublishErrorOut(CamelModel):
    code: str
    message: str
    occurred_at: datetime


class PostMediaOut(CamelModel):
    id: str
    kind: Literal["image", "video"]
    url: str
    alt: str
    library_id: str | None = None


class PostOut(CamelModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    brand_id: uuid.UUID
    group_id: uuid.UUID | None = None
    title: str | None = None
    internal_note: str | None = None
    pillar_id: uuid.UUID | None = None
    platform: Platform
    status: PostStatus
    disclosure: DisclosureIn | None = None
    scheduled_at: datetime
    first_comment: str | None = None
    published_at: datetime | None = None
    submitted_at: datetime | None = None
    approved_at: datetime | None = None
    variants: list[PostVariantIn]
    media: list[PostMediaOut]
    risk: RiskReportOut
    cultural_event_id: uuid.UUID | None = None
    change_request_reason: str | None = None
    reject_reason: str | None = None
    last_error: PublishErrorOut | None = None
    created_by: uuid.UUID
    approved_by: uuid.UUID | None = None
    ai_decision_id: uuid.UUID | None = None
    version: int
    created_at: datetime
    updated_at: datetime


class CreatePostBody(CamelModel):
    platform: Platform
    scheduled_at: datetime
    first_comment: str | None = None
    variants: list[PostVariantIn] = Field(min_length=1)
    media: list[PostMediaIn] | None = None
    pillar_id: uuid.UUID | None = None
    cultural_event_id: uuid.UUID | None = None
    group_id: uuid.UUID | None = None
    ai_decision_id: uuid.UUID | None = None
    disclosure: DisclosureIn | None = None


class UpdatePostBody(CamelModel):
    platform: Platform | None = None
    first_comment: str | None = None
    variants: list[PostVariantIn] | None = None
    media: list[PostMediaIn] | None = None
    scheduled_at: datetime | None = None
    pillar_id: uuid.UUID | None = None
    cultural_event_id: uuid.UUID | None = None
    internal_note: str | None = None
    disclosure: DisclosureIn | None = None
    # Optimistic concurrency — optional on the wire today; when present, CAS
    # against this value. Architecture/04 requires it; the Zod contract has not
    # caught up yet.
    version: int | None = Field(default=None, gt=0)


class ReasonBody(CamelModel):
    reason: str = Field(min_length=1)


class AddCommentBody(CamelModel):
    body: str = Field(min_length=1)
    lang: Lang


class PostVersionSnapshotOut(CamelModel):
    variants: list[PostVariantIn]
    media: list[PostMediaOut]
    scheduled_at: datetime
    platform: Platform
    pillar_id: uuid.UUID | None = None


class PostVersionOut(CamelModel):
    id: uuid.UUID
    post_id: uuid.UUID
    version: int
    reason: VersionReason
    author_id: uuid.UUID
    author_name: str
    created_at: datetime
    snapshot: PostVersionSnapshotOut


class PostCommentOut(CamelModel):
    id: uuid.UUID
    post_id: uuid.UUID
    author_id: uuid.UUID
    author_name: str
    body: str
    lang: Lang
    resolved: bool
    created_at: datetime


class BulkApproveBody(CamelModel):
    post_ids: list[uuid.UUID] = Field(min_length=1)


class BulkApproveItem(CamelModel):
    id: uuid.UUID
    ok: bool


class RescheduleBody(CamelModel):
    date_key: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class RescheduleResponse(CamelModel):
    post: PostOut
    warnings: list[Literal["conflict", "past"]]


def risk_to_out(risk: dict[str, Any] | RiskReportOut) -> RiskReportOut:
    if isinstance(risk, RiskReportOut):
        return risk
    return RiskReportOut.model_validate(risk)
