from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from app.core.schema import CamelModel
from app.features.posts.schemas import Platform, PostMediaOut, PostVariantIn

Locale = Literal["ar", "en"]
ReviewDecision = Literal["approved", "changes_requested"]


class CreateReviewLinkBody(CamelModel):
    expires_in_days: int = Field(default=7, ge=1, le=30)
    locale: Locale = "ar"


class CreateReviewLinkResponse(CamelModel):
    token: str
    url: str
    expires_at: datetime


class PublicReviewPostOut(CamelModel):
    """Guest-facing projection — deliberately smaller than PostOut."""

    platform: Platform
    variants: list[PostVariantIn]
    media: list[PostMediaOut]
    first_comment: str | None = None
    scheduled_at: datetime
    scheduled_date_key: str
    cultural_event_name: str | None = None


class ReviewLinkOut(CamelModel):
    token: str
    brand_name: str
    brand_logo_url: str | None = None
    locale: Locale
    expires_at: datetime
    decision: ReviewDecision | None = None
    decided_at: datetime | None = None
    decided_by_name: str | None = None
    posts: list[PublicReviewPostOut] = Field(min_length=1)


class ReviewDecisionBody(CamelModel):
    decision: ReviewDecision
    reviewer_name: str = Field(min_length=1, max_length=80)
    comment: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _comment_required_for_changes(self) -> ReviewDecisionBody:
        if self.decision == "changes_requested":
            if not (self.comment and self.comment.strip()):
                raise ValueError("comment is required when requesting changes")
        return self


class ReviewDecisionResponse(CamelModel):
    decision: ReviewDecision
    decided_at: datetime
