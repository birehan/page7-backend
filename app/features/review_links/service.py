from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.time import riyadh_date_key, utc_now
from app.features import audit, brands, posts
from app.features import cultural_events as cultural_events_feature
from app.features import notifications as notifications_feature
from app.features.review_links import repository
from app.features.review_links.models import ReviewLink
from app.features.review_links.schemas import (
    CreateReviewLinkBody,
    CreateReviewLinkResponse,
    Locale,
    PublicReviewPostOut,
    ReviewDecision,
    ReviewDecisionBody,
    ReviewDecisionResponse,
    ReviewLinkOut,
)
from app.infrastructure.ratelimit.limiter import RateLimiter
from app.integrations.storage.ports import ObjectStorage

_TOKEN_BYTES = 18  # token_urlsafe(18) → 24 URL-safe characters
_VIEW_LIMIT = 60
_VIEW_WINDOW = timedelta(minutes=15)
_DECISION_LIMIT = 10
_DECISION_WINDOW = timedelta(minutes=15)
_IP_VIEW_LIMIT = 120
_IP_DECISION_LIMIT = 20


def _new_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def assert_link_active(link: ReviewLink | None) -> None:
    if link is None:
        raise ApiError("NOT_FOUND", "This link is not valid.", status_code=404)
    if link.revoked_at is not None:
        raise ApiError("NOT_FOUND", "This link is not valid.", status_code=404)
    if link.expires_at <= utc_now():
        raise ApiError("NOT_FOUND", "This link is not valid.", status_code=404)


async def rate_limit_guest(
    *,
    action: str,
    token: str,
    ip: str | None,
) -> None:
    """Per-token and per-IP buckets, distinct from login lockout buckets."""
    limiter = RateLimiter()
    now = utc_now()
    token_hash = hash_token(token)
    window = _VIEW_WINDOW if action == "view" else _DECISION_WINDOW
    token_limit = _VIEW_LIMIT if action == "view" else _DECISION_LIMIT
    ip_limit = _IP_VIEW_LIMIT if action == "view" else _IP_DECISION_LIMIT

    token_count = await limiter.increment(
        bucket=f"review:{action}:token:{token_hash}",
        now=now,
        window=window,
    )
    ip_count = 0
    if ip:
        ip_count = await limiter.increment(
            bucket=f"review:{action}:ip:{ip}",
            now=now,
            window=window,
        )
    if token_count > token_limit or ip_count > ip_limit:
        raise ApiError("RATE_LIMIT", "Too many requests", status_code=429)


async def create_review_link(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    body: CreateReviewLinkBody,
    actor_user_id: uuid.UUID,
    actor_name: str,
    storage: ObjectStorage,
) -> CreateReviewLinkResponse:
    group = await posts.get_post_group(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
        storage=storage,
    )
    if not group:
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)

    raw_token = _new_token()
    expires_at = utc_now() + timedelta(days=body.expires_in_days)
    await repository.create_review_link(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        token_hash=hash_token(raw_token),
        locale=body.locale,
        created_by=actor_user_id,
        expires_at=expires_at,
        post_ids=[p.id for p in group],
    )
    await audit.record(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="review_link.created",
        target_type="post",
        target_id=post_id,
        meta={"expiresAt": expires_at.isoformat().replace("+00:00", "Z")},
    )
    frontend = get_settings().auth.frontend_url.rstrip("/")
    url = f"{frontend}/{body.locale}/review/{raw_token}"
    return CreateReviewLinkResponse(token=raw_token, url=url, expires_at=expires_at)


async def _public_post(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    locale: str,
    storage: ObjectStorage,
) -> PublicReviewPostOut | None:
    post = await posts.get_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
        storage=storage,
    )
    if post is None:
        return None

    cultural_event_name: str | None = None
    if post.cultural_event_id is not None:
        event = await cultural_events_feature.get_cultural_event(
            session, event_id=post.cultural_event_id
        )
        if event is not None:
            cultural_event_name = event.name_ar if locale == "ar" else event.name

    return PublicReviewPostOut(
        platform=post.platform,
        variants=post.variants,
        media=post.media,
        first_comment=post.first_comment,
        scheduled_at=post.scheduled_at,
        scheduled_date_key=riyadh_date_key(post.scheduled_at),
        cultural_event_name=cultural_event_name,
    )


async def get_public_review_link(
    session: AsyncSession,
    *,
    token: str,
    ip: str | None,
    storage: ObjectStorage,
) -> ReviewLinkOut:
    await rate_limit_guest(action="view", token=token, ip=ip)
    link = await repository.get_by_token_hash(session, token_hash=hash_token(token))
    assert_link_active(link)
    assert link is not None

    await repository.record_view(session, review_link_id=link.id)

    brand = await brands.get_brand(
        session, organization_id=link.organization_id, brand_id=link.brand_id
    )
    if brand is None:
        raise ApiError("NOT_FOUND", "This link is not valid.", status_code=404)

    frozen_ids = await repository.list_frozen_post_ids(
        session, review_link_id=link.id
    )
    public_posts: list[PublicReviewPostOut] = []
    for post_id in frozen_ids:
        projection = await _public_post(
            session,
            organization_id=link.organization_id,
            brand_id=link.brand_id,
            post_id=post_id,
            locale=link.locale,
            storage=storage,
        )
        if projection is not None:
            public_posts.append(projection)

    if not public_posts:
        raise ApiError("NOT_FOUND", "This link is not valid.", status_code=404)

    return ReviewLinkOut(
        token=token,
        brand_name=brand.name,
        brand_logo_url=brand.logo_url,
        locale=cast(Locale, link.locale),
        expires_at=link.expires_at,
        decision=cast(ReviewDecision | None, link.decision),
        decided_at=link.decided_at,
        decided_by_name=link.decided_by_name,
        posts=public_posts,
    )


async def submit_decision(
    session: AsyncSession,
    *,
    token: str,
    body: ReviewDecisionBody,
    ip: str | None,
    storage: ObjectStorage,
    skip_rate_limit: bool = False,
) -> ReviewDecisionResponse:
    if not skip_rate_limit:
        await rate_limit_guest(action="decision", token=token, ip=ip)
    link = await repository.get_by_token_hash(session, token_hash=hash_token(token))
    assert_link_active(link)
    assert link is not None

    if link.decision is not None:
        raise ApiError(
            "ALREADY_DECIDED",
            "A decision has already been recorded for this link.",
            status_code=409,
        )

    reviewer_name = body.reviewer_name.strip()
    comment = body.comment.strip() if body.comment else None
    actor = posts.guest_actor(review_link_id=link.id, name=reviewer_name)
    frozen_ids = await repository.list_frozen_post_ids(
        session, review_link_id=link.id
    )
    decided_at = utc_now()

    for post_id in frozen_ids:
        if body.decision == "approved":
            await posts.approve_via_review_link(
                session,
                organization_id=link.organization_id,
                brand_id=link.brand_id,
                post_id=post_id,
                review_link_id=link.id,
                actor=actor,
                storage=storage,
            )
        else:
            assert comment is not None
            await posts.request_changes_via_review_link(
                session,
                organization_id=link.organization_id,
                brand_id=link.brand_id,
                post_id=post_id,
                reason=comment,
                actor=actor,
                storage=storage,
            )

    await repository.record_decision(
        session,
        review_link_id=link.id,
        decision=body.decision,
        decided_at=decided_at,
        decided_by_name=reviewer_name,
        decision_comment=comment,
        decided_ip=ip,
    )

    brand = await brands.get_brand(
        session, organization_id=link.organization_id, brand_id=link.brand_id
    )
    brand_name = brand.name if brand is not None else ""

    await audit.record(
        session,
        organization_id=link.organization_id,
        brand_id=link.brand_id,
        actor_kind="guest",
        actor_ref=actor.ref,
        actor_name=actor.name,
        actor_user_id=None,
        action=f"review_link.{body.decision}",
        target_type="post",
        target_id=frozen_ids[0],
        meta={"reviewerName": reviewer_name, "external": True},
    )

    notif_type = (
        "post_approved" if body.decision == "approved" else "changes_requested"
    )
    await notifications_feature.fan_out(
        session,
        organization_id=link.organization_id,
        brand_id=link.brand_id,
        type=notif_type,
        message_key=notif_type,
        params={"actor": actor.name, "title": brand_name},
        target_href="/approvals",
        actor_user_id=None,
        actor_ref=actor.ref,
    )

    return ReviewDecisionResponse(decision=body.decision, decided_at=decided_at)
