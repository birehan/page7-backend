"""Publishing service — claim checks + apply_publish_result (architecture/10 §6)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import riyadh_day_start_utc, utc_now
from app.features import audit, notifications, organizations
from app.features import media as media_feature
from app.features.posts import Post, PostMedia
from app.features.publishing import repository
from app.features.publishing.exceptions import (
    ClaimCheckFailed,
    DoublePostDetected,
    NonDefinitiveOutcome,
)
from app.features.publishing.models import Publication
from app.features.social_accounts import (
    SocialAccount,
    capability_matrix,
    get_account,
    get_account_by_platform,
    get_credential,
)
from app.integrations.social.ports import PlatformOutcome, PublishResult
from app.integrations.storage.ports import ObjectStorage

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _SystemActor:
    name: str
    ref: str
    kind: str
    user_id: uuid.UUID | None


SYSTEM_ACTOR = _SystemActor(
    name="Publishing pipeline",
    ref="system:publishing",
    kind="system",
    user_id=None,
)

HOURLY_VELOCITY_LIMIT = 25
DAILY_VELOCITY_LIMITS: dict[str, int] = {
    "instagram": 100,
    "facebook": 100,
    "tiktok": 50,
    "snapchat": 50,
}

# Auto-retry publication attempt ceilings (architecture/10 §6).
_MAX_AUTO_RETRIES_PLATFORM = 3
_MAX_AUTO_RETRIES_UNKNOWN = 2  # retry once, then terminal
_AUTO_RETRY_DELAYS_SECONDS = (60, 180, 360)

_TERMINAL_PUBLICATION = frozenset({"published", "failed", "skipped", "cancelled"})

PublicationErrorCategory = Literal[
    "auth",
    "rate_limit",
    "validation",
    "platform",
    "network",
    "frozen",
    "internal",
]


@dataclass(frozen=True)
class MappedError:
    """Zernio errorCategory → our lastError.code + publication category + retry."""

    code: str
    publication_category: PublicationErrorCategory
    retryable: bool
    max_attempts: int  # inclusive attempt_no ceiling for auto-retry
    mark_account_expired: bool = False
    default_message: str = ""


@dataclass(frozen=True)
class ApplyPublishResultOutcome:
    """Structured result for job/webhook/reconcile callers."""

    publication_status: str
    post_status: str | None
    finalized: bool
    should_retry_job: bool
    retry_after_seconds: float | None
    should_auto_retry_publication: bool
    error_code: str | None = None
    terminal: bool = False


_ERROR_CATEGORY_MAP: dict[str, MappedError] = {
    "user_content": MappedError(
        code="PLATFORM_REJECTED",
        publication_category="validation",
        retryable=False,
        max_attempts=0,
        default_message="The platform rejected this post's content",
    ),
    "platform_rejected": MappedError(
        code="PLATFORM_REJECTED",
        publication_category="validation",
        retryable=False,
        max_attempts=0,
        default_message="The platform rejected this post",
    ),
    "user_abuse": MappedError(
        code="PLATFORM_REJECTED",
        publication_category="validation",
        retryable=False,
        max_attempts=0,
        default_message="The platform flagged this post as abusive",
    ),
    "auth_expired": MappedError(
        code="ACCOUNT_TOKEN_EXPIRED",
        publication_category="auth",
        retryable=False,
        max_attempts=0,
        mark_account_expired=True,
        default_message="The connected account token has expired",
    ),
    "account_issue": MappedError(
        code="ACCOUNT_ISSUE",
        publication_category="auth",
        retryable=False,
        max_attempts=0,
        default_message="The connected account has an issue that blocks publishing",
    ),
    "platform_error": MappedError(
        code="PLATFORM_ERROR",
        publication_category="platform",
        retryable=True,
        max_attempts=_MAX_AUTO_RETRIES_PLATFORM,
        default_message="The platform returned a transient error",
    ),
    "system_error": MappedError(
        code="PROVIDER_ERROR",
        publication_category="network",
        retryable=True,
        max_attempts=_MAX_AUTO_RETRIES_PLATFORM,
        default_message="The social provider returned a system error",
    ),
    "platform_rate_limit": MappedError(
        code="PLATFORM_RATE_LIMIT",
        publication_category="rate_limit",
        retryable=True,
        max_attempts=_MAX_AUTO_RETRIES_PLATFORM,
        default_message="The platform rate-limited this publish",
    ),
    "quota_exhausted": MappedError(
        code="QUOTA_EXHAUSTED",
        publication_category="rate_limit",
        retryable=False,
        max_attempts=0,
        default_message="The provider quota for this account is exhausted",
    ),
    "unknown": MappedError(
        code="PROVIDER_ERROR",
        publication_category="network",
        retryable=True,
        max_attempts=_MAX_AUTO_RETRIES_UNKNOWN,
        default_message="The provider returned an unknown error",
    ),
}


def map_error_category(zernio_category: str | None) -> MappedError:
    """Map a Zernio errorCategory to our code / retry policy (architecture/10 §6)."""
    if zernio_category is None or zernio_category not in _ERROR_CATEGORY_MAP:
        return _ERROR_CATEGORY_MAP["unknown"]
    return _ERROR_CATEGORY_MAP[zernio_category]


def check_lateness(
    run_at: datetime,
    *,
    now: datetime,
    window_seconds: int = 1800,
) -> bool:
    """Return True when the job missed its lateness window (architecture/10 §5)."""
    if run_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("run_at and now must be timezone-aware")
    return (now - run_at).total_seconds() > window_seconds


def compute_velocity_hold_until(
    *,
    platform: str,
    now: datetime,
    hourly_count: int,
    hourly_oldest: datetime | None,
    daily_count: int,
    daily_oldest: datetime | None,
    hourly_limit: int = HOURLY_VELOCITY_LIMIT,
    daily_limits: dict[str, int] | None = None,
) -> datetime | None:
    """Next run_at when velocity caps are exceeded; None if publish may proceed."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    limits = daily_limits if daily_limits is not None else DAILY_VELOCITY_LIMITS
    held_until: datetime | None = None

    if hourly_count >= hourly_limit:
        if hourly_oldest is None:
            held_until = now + timedelta(hours=1)
        else:
            candidate = hourly_oldest + timedelta(hours=1)
            held_until = candidate if held_until is None else max(held_until, candidate)

    daily_limit = limits.get(platform)
    if daily_limit is not None and daily_count >= daily_limit:
        # Next Riyadh midnight — daily caps reset on the Riyadh calendar day.
        day_start = riyadh_day_start_utc(now)
        next_midnight = day_start + timedelta(days=1)
        if daily_oldest is not None:
            # Also wait until the oldest in-window publish ages out of the day.
            aged = daily_oldest + timedelta(days=1)
            next_midnight = max(next_midnight, aged)
        held_until = (
            next_midnight if held_until is None else max(held_until, next_midnight)
        )

    return held_until


def auto_retry_delay_seconds(attempt_no: int) -> float:
    """Delay before the next auto-retry publication (1m / 3m / 6m)."""
    idx = max(0, min(attempt_no - 1, len(_AUTO_RETRY_DELAYS_SECONDS) - 1))
    return float(_AUTO_RETRY_DELAYS_SECONDS[idx])


def _iso_z(instant: datetime) -> str:
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _last_error(code: str, message: str, *, when: datetime | None = None) -> dict[str, str]:
    return {
        "code": code,
        "message": message,
        "occurredAt": _iso_z(when or utc_now()),
    }


def _platform_for_account(
    platforms: list[PlatformOutcome], account_id: str | None
) -> PlatformOutcome | None:
    if not platforms:
        return None
    if account_id:
        for row in platforms:
            if row.account_id == account_id:
                return row
    return platforms[0]


# ---------------------------------------------------------------------------
# Claim-side helpers
# ---------------------------------------------------------------------------


async def check_freeze(session: AsyncSession, organization_id: uuid.UUID) -> bool:
    settings = await organizations.get_org_settings(session, organization_id)
    return bool(settings.publishing_frozen)


async def resolve_social_account_for_post(
    session: AsyncSession, post: Post
) -> SocialAccount:
    """Load the brand+platform account; must be connected, publishable, credential enabled."""
    account = await get_account_by_platform(
        session,
        organization_id=post.organization_id,
        brand_id=post.brand_id,
        platform=post.platform,
    )
    if account is None or account.status == "disconnected":
        raise ClaimCheckFailed(
            "No connected channel for this platform",
            code="ACCOUNT_DISCONNECTED",
        )
    if account.status == "expired":
        raise ClaimCheckFailed(
            "The connected account token has expired",
            code="ACCOUNT_TOKEN_EXPIRED",
        )
    if account.status not in ("connected", "expiring"):
        raise ClaimCheckFailed(
            "The connected account cannot publish right now",
            code="ACCOUNT_DISCONNECTED",
        )

    matrix = capability_matrix()
    publishable = next(
        (p.publishable for p in matrix.platforms if p.platform == post.platform),
        False,
    )
    if not publishable:
        raise ClaimCheckFailed(
            f"Publishing to {post.platform} is not enabled",
            code="CHANNEL_NOT_CONNECTED",
        )

    if account.credential_id is None or account.zernio_profile_id is None:
        raise ClaimCheckFailed(
            "Social account is missing provider pins",
            code="ACCOUNT_DISCONNECTED",
        )

    credential = await get_credential(
        session, credential_id=account.credential_id
    )
    if credential is None or credential.status == "disabled":
        raise ClaimCheckFailed(
            "The provider credential for this account is disabled",
            code="ACCOUNT_DISCONNECTED",
        )
    if credential.status not in ("active", "draining"):
        raise ClaimCheckFailed(
            "The provider credential is not available",
            code="ACCOUNT_DISCONNECTED",
        )

    return account


async def check_media_reachable(
    session: AsyncSession,
    post: Post,
    storage: ObjectStorage,
) -> None:
    """HeadObject each attached asset; raise MEDIA_UNAVAILABLE / MEDIA_NOT_PUBLISHABLE."""
    media_rows = (
        await session.execute(
            select(PostMedia)
            .where(PostMedia.post_id == post.id)
            .order_by(PostMedia.position.asc())
        )
    ).scalars().all()
    if not media_rows:
        return

    asset_ids = [row.media_asset_id for row in media_rows]
    assets = await media_feature.get_assets_by_ids(
        session,
        organization_id=post.organization_id,
        brand_id=post.brand_id,
        asset_ids=asset_ids,
    )
    by_id = {asset.id: asset for asset in assets}
    for row in media_rows:
        asset = by_id.get(row.media_asset_id)
        if asset is None:
            raise ClaimCheckFailed(
                "A media asset attached to this post is missing",
                code="MEDIA_UNAVAILABLE",
            )
        if asset.kind == "template":
            raise ClaimCheckFailed(
                "Template media must be rendered before publishing",
                code="MEDIA_NOT_PUBLISHABLE",
            )
        try:
            head = await storage.head_object("public", asset.r2_key)
        except Exception as exc:
            logger.warning(
                "media_head_failed",
                post_id=str(post.id),
                media_asset_id=str(asset.id),
                error=str(exc),
            )
            raise ClaimCheckFailed(
                "Media could not be reached in storage",
                code="MEDIA_UNAVAILABLE",
            ) from exc
        if not head.exists:
            raise ClaimCheckFailed(
                "Media object is missing from storage",
                code="MEDIA_UNAVAILABLE",
            )


async def check_velocity(
    session: AsyncSession,
    social_account_id: uuid.UUID,
    platform: str,
    *,
    now: datetime | None = None,
) -> datetime | None:
    """Return next run_at if held by velocity caps; None if ok to publish."""
    instant = now or utc_now()
    hour_ago = instant - timedelta(hours=1)
    day_start = riyadh_day_start_utc(instant)

    hourly_count = await repository.count_published_in_window(
        session, social_account_id=social_account_id, since=hour_ago
    )
    hourly_oldest = await repository.oldest_published_completed_at(
        session, social_account_id=social_account_id, since=hour_ago
    )
    daily_count = await repository.count_published_in_window(
        session, social_account_id=social_account_id, since=day_start
    )
    daily_oldest = await repository.oldest_published_completed_at(
        session, social_account_id=social_account_id, since=day_start
    )
    return compute_velocity_hold_until(
        platform=platform,
        now=instant,
        hourly_count=hourly_count,
        hourly_oldest=hourly_oldest,
        daily_count=daily_count,
        daily_oldest=daily_oldest,
    )


async def next_attempt_no(session: AsyncSession, post_id: uuid.UUID) -> int:
    return await repository.next_attempt_no(session, post_id)


async def get_inflight_publication(
    session: AsyncSession, post_id: uuid.UUID
) -> Publication | None:
    return await repository.get_inflight_by_post(session, post_id)


async def get_latest_publication(
    session: AsyncSession, post_id: uuid.UUID
) -> Publication | None:
    return await repository.get_latest_by_post(session, post_id)


async def insert_publication_sent(
    session: AsyncSession,
    *,
    post: Post,
    social_account: SocialAccount,
    attempt_no: int,
    trigger: str,
    job_id: int | None = None,
    request_payload: dict[str, Any] | None = None,
) -> Publication:
    if social_account.zernio_profile_id is None or social_account.credential_id is None:
        raise ClaimCheckFailed(
            "Social account is missing provider pins",
            code="ACCOUNT_DISCONNECTED",
        )
    return await repository.insert_publication(
        session,
        organization_id=post.organization_id,
        brand_id=post.brand_id,
        post_id=post.id,
        social_account_id=social_account.id,
        zernio_profile_id=social_account.zernio_profile_id,
        credential_id=social_account.credential_id,
        attempt_no=attempt_no,
        trigger=trigger,
        idempotency_key=f"pub:{post.id}:{attempt_no}",
        scheduled_for=post.scheduled_at,
        job_id=job_id,
        status="sent",
        request_payload=request_payload,
    )


async def _cas_post(
    session: AsyncSession,
    post: Post,
    *,
    from_status: str,
    to_status: str,
    patch: dict[str, Any] | None = None,
) -> Post:
    updated = await repository.cas_post_status(
        session,
        post_id=post.id,
        brand_id=post.brand_id,
        from_status=from_status,
        expected_version=post.version,
        to_status=to_status,
        patch=patch,
    )
    if updated is None:
        raise ClaimCheckFailed(
            f"Could not transition post from {from_status} to {to_status}",
            code="INVALID_TRANSITION",
        )
    return updated


async def fail_post_at_claim(
    session: AsyncSession,
    post: Post,
    *,
    code: str,
    message: str,
) -> Post:
    """Terminal claim failure with no publication row.

    ``scheduled → publishing → failed`` in one transaction so we stay inside
    ``post_status_transitions`` (scheduled cannot go directly to failed).
    """
    error = _last_error(code, message)
    current = post
    if current.status == "scheduled":
        current = await _cas_post(
            session, current, from_status="scheduled", to_status="publishing"
        )
    if current.status != "publishing":
        raise ClaimCheckFailed(
            f"Cannot mark post failed from status {current.status}",
            code="INVALID_TRANSITION",
        )
    failed = await _cas_post(
        session,
        current,
        from_status="publishing",
        to_status="failed",
        patch={"last_error": error},
    )
    await _notify_and_audit_failure(
        session,
        post=failed,
        code=code,
        message=message,
        publication_id=None,
    )
    return failed


async def skip_publication_for_freeze(
    session: AsyncSession,
    *,
    publication: Publication,
    post: Post,
) -> ApplyPublishResultOutcome:
    """Second freeze check after insert: publication → skipped, post → failed."""
    now = utc_now()
    publication.status = "skipped"
    publication.completed_at = now
    publication.error_code = "FROZEN"
    publication.error_category = "frozen"
    publication.error_message = "Publishing is frozen for this organization"
    publication.retryable = False
    await session.flush()

    error = _last_error("FROZEN", publication.error_message, when=now)
    failed = post
    if post.status == "publishing":
        failed = await _cas_post(
            session,
            post,
            from_status="publishing",
            to_status="failed",
            patch={"last_error": error},
        )
    await _notify_and_audit_failure(
        session,
        post=failed,
        code="FROZEN",
        message=publication.error_message,
        publication_id=publication.id,
    )
    return ApplyPublishResultOutcome(
        publication_status="skipped",
        post_status="failed",
        finalized=True,
        should_retry_job=False,
        retry_after_seconds=None,
        should_auto_retry_publication=False,
        error_code="FROZEN",
        terminal=True,
    )


async def mark_post_publishing(session: AsyncSession, post: Post) -> Post:
    """CAS scheduled → publishing (bypasses transition freeze re-check)."""
    if post.status == "publishing":
        return post
    return await _cas_post(
        session, post, from_status="scheduled", to_status="publishing"
    )


# ---------------------------------------------------------------------------
# apply_publish_result
# ---------------------------------------------------------------------------


async def apply_publish_result(
    session: AsyncSession,
    publication_id: uuid.UUID,
    outcome: PublishResult,
    *,
    actor_kind: Literal["system", "user", "policy", "guest"] = "system",
    actor_ref: str = SYSTEM_ACTOR.ref,
    actor_name: str = SYSTEM_ACTOR.name,
    actor_user_id: uuid.UUID | None = None,
) -> ApplyPublishResultOutcome:
    """Single choke point for job, webhook, and reconcile result application."""
    del actor_kind  # always system for automated paths; kept for call-site clarity
    publication = await repository.get_publication_for_update(session, publication_id)
    if publication is None:
        raise ClaimCheckFailed(
            f"Publication {publication_id} not found",
            code="NOT_FOUND",
        )

    # Monotonic: never regress a terminal publication (except failed→published).
    if (
        publication.status in _TERMINAL_PUBLICATION
        and publication.status != "failed"
    ):
        logger.info(
            "apply_publish_result_noop_terminal",
            publication_id=str(publication_id),
            status=publication.status,
            outcome_kind=outcome.kind,
        )
        return ApplyPublishResultOutcome(
            publication_status=publication.status,
            post_status=None,
            finalized=True,
            should_retry_job=False,
            retry_after_seconds=None,
            should_auto_retry_publication=False,
            error_code=publication.error_code,
            terminal=True,
        )

    post = await session.get(Post, publication.post_id)
    if post is None:
        raise ClaimCheckFailed("Post not found for publication", code="NOT_FOUND")

    account = await get_account(
        session,
        organization_id=publication.organization_id,
        brand_id=publication.brand_id,
        account_id=publication.social_account_id,
    )
    zernio_account_id = account.zernio_account_id if account is not None else None

    if outcome.kind == "non_definitive":
        # Leave publication as sent; caller raises RetryableError.
        logger.info(
            "apply_publish_result_non_definitive",
            publication_id=str(publication_id),
            post_id=str(post.id),
        )
        raise NonDefinitiveOutcome(
            outcome.error_message or "Publish outcome is not yet definitive",
            retry_after_seconds=outcome.retry_after or 15.0,
        )

    if outcome.kind in ("created", "existing"):
        return await _apply_created_or_existing(
            session,
            publication=publication,
            post=post,
            outcome=outcome,
            zernio_account_id=zernio_account_id,
            actor_ref=actor_ref,
            actor_name=actor_name,
            actor_user_id=actor_user_id,
        )

    if outcome.kind == "duplicate_conflict":
        return await _apply_duplicate_conflict(
            session,
            publication=publication,
            post=post,
            outcome=outcome,
            zernio_account_id=zernio_account_id,
            actor_ref=actor_ref,
            actor_name=actor_name,
            actor_user_id=actor_user_id,
        )

    if outcome.kind == "rate_limited":
        mapped = map_error_category("platform_rate_limit")
        return await _finalize_retryable_or_terminal(
            session,
            publication=publication,
            post=post,
            mapped=mapped,
            message=outcome.error_message or mapped.default_message,
            retry_after_seconds=outcome.retry_after,
            response_payload=outcome.raw_response,
            actor_ref=actor_ref,
            actor_name=actor_name,
            actor_user_id=actor_user_id,
        )

    if outcome.kind == "auth_failed":
        return await _finalize_terminal_failure(
            session,
            publication=publication,
            post=post,
            code="PROVIDER_UNAVAILABLE",
            category="auth",
            message=outcome.error_message or "Provider authentication failed",
            response_payload=outcome.raw_response,
            actor_ref=actor_ref,
            actor_name=actor_name,
            actor_user_id=actor_user_id,
        )

    if outcome.kind == "payment_required":
        return await _finalize_terminal_failure(
            session,
            publication=publication,
            post=post,
            code="PROVIDER_PAYMENT_REQUIRED",
            category="auth",
            message=outcome.error_message or "Provider payment required",
            response_payload=outcome.raw_response,
            actor_ref=actor_ref,
            actor_name=actor_name,
            actor_user_id=actor_user_id,
        )

    if outcome.kind == "rejected":
        mapped = map_error_category(outcome.error_category)
        message = outcome.error_message or mapped.default_message
        if mapped.mark_account_expired and account is not None:
            account.status = "expired"
            account.last_error_code = "ACCOUNT_TOKEN_EXPIRED"
            account.last_error_at = utc_now()
            await session.flush()
        if mapped.retryable:
            return await _finalize_retryable_or_terminal(
                session,
                publication=publication,
                post=post,
                mapped=mapped,
                message=message,
                retry_after_seconds=outcome.retry_after,
                response_payload=outcome.raw_response,
                actor_ref=actor_ref,
                actor_name=actor_name,
                actor_user_id=actor_user_id,
            )
        return await _finalize_terminal_failure(
            session,
            publication=publication,
            post=post,
            code=mapped.code,
            category=mapped.publication_category,
            message=message,
            response_payload=outcome.raw_response,
            actor_ref=actor_ref,
            actor_name=actor_name,
            actor_user_id=actor_user_id,
        )

    # Defensive fallback for unexpected kinds.
    return await _finalize_terminal_failure(
        session,
        publication=publication,
        post=post,
        code="PROVIDER_ERROR",
        category="network",
        message=outcome.error_message or f"Unhandled publish outcome: {outcome.kind}",
        response_payload=outcome.raw_response,
        actor_ref=actor_ref,
        actor_name=actor_name,
        actor_user_id=actor_user_id,
    )


async def _apply_created_or_existing(
    session: AsyncSession,
    *,
    publication: Publication,
    post: Post,
    outcome: PublishResult,
    zernio_account_id: str | None,
    actor_ref: str,
    actor_name: str,
    actor_user_id: uuid.UUID | None,
) -> ApplyPublishResultOutcome:
    zernio_post_id = outcome.zernio_post_id
    if not zernio_post_id:
        return await _finalize_terminal_failure(
            session,
            publication=publication,
            post=post,
            code="PROVIDER_ERROR",
            category="network",
            message="Provider returned success without a post id",
            response_payload=outcome.raw_response,
            actor_ref=actor_ref,
            actor_name=actor_name,
            actor_user_id=actor_user_id,
        )

    publication.zernio_post_id = zernio_post_id
    publication.response_payload = outcome.raw_response
    platform_row = _platform_for_account(list(outcome.platforms), zernio_account_id)
    status = platform_row.status if platform_row is not None else "publishing"

    if status in ("pending", "publishing"):
        publication.status = "accepted"
        if platform_row is not None:
            publication.external_post_id = platform_row.platform_post_id
            publication.external_url = platform_row.platform_post_url
        await session.flush()
        return ApplyPublishResultOutcome(
            publication_status="accepted",
            post_status="publishing",
            finalized=False,
            should_retry_job=False,
            retry_after_seconds=None,
            should_auto_retry_publication=False,
        )

    if status == "published":
        return await _finalize_published(
            session,
            publication=publication,
            post=post,
            platform_row=platform_row,
            actor_ref=actor_ref,
            actor_name=actor_name,
            actor_user_id=actor_user_id,
        )

    # platform failed
    category = platform_row.error_category if platform_row is not None else None
    mapped = map_error_category(category)
    message = (
        (platform_row.error_message if platform_row is not None else None)
        or outcome.error_message
        or mapped.default_message
    )
    if mapped.retryable:
        return await _finalize_retryable_or_terminal(
            session,
            publication=publication,
            post=post,
            mapped=mapped,
            message=message,
            retry_after_seconds=outcome.retry_after,
            response_payload=outcome.raw_response,
            actor_ref=actor_ref,
            actor_name=actor_name,
            actor_user_id=actor_user_id,
        )
    return await _finalize_terminal_failure(
        session,
        publication=publication,
        post=post,
        code=mapped.code,
        category=mapped.publication_category,
        message=message,
        response_payload=outcome.raw_response,
        actor_ref=actor_ref,
        actor_name=actor_name,
        actor_user_id=actor_user_id,
    )


async def _apply_duplicate_conflict(
    session: AsyncSession,
    *,
    publication: Publication,
    post: Post,
    outcome: PublishResult,
    zernio_account_id: str | None,
    actor_ref: str,
    actor_name: str,
    actor_user_id: uuid.UUID | None,
) -> ApplyPublishResultOutcome:
    existing_id = outcome.existing_post_id or outcome.zernio_post_id
    if existing_id:
        prior = await repository.get_by_zernio_post_id(session, existing_id)
        if prior is not None and prior.post_id == post.id:
            # Crash-recovery: adopt the existing Zernio post.
            adopted = PublishResult(
                kind="existing",
                zernio_post_id=existing_id,
                platforms=outcome.platforms,
                raw_response=outcome.raw_response,
                http_status=outcome.http_status,
            )
            return await _apply_created_or_existing(
                session,
                publication=publication,
                post=post,
                outcome=adopted,
                zernio_account_id=zernio_account_id,
                actor_ref=actor_ref,
                actor_name=actor_name,
                actor_user_id=actor_user_id,
            )

    return await _finalize_terminal_failure(
        session,
        publication=publication,
        post=post,
        code="DUPLICATE_CONTENT",
        category="validation",
        message=outcome.error_message
        or "Provider rejected this publish as duplicate content",
        response_payload=outcome.raw_response,
        actor_ref=actor_ref,
        actor_name=actor_name,
        actor_user_id=actor_user_id,
    )


async def _finalize_published(
    session: AsyncSession,
    *,
    publication: Publication,
    post: Post,
    platform_row: PlatformOutcome | None,
    actor_ref: str,
    actor_name: str,
    actor_user_id: uuid.UUID | None,
) -> ApplyPublishResultOutcome:
    now = utc_now()
    was_failed = publication.status == "failed"

    # ux_pub_published: at most one published row per post. A late
    # failed→published correction that would collide must alert, not overwrite.
    other = (
        await session.execute(
            select(Publication.id).where(
                Publication.post_id == post.id,
                Publication.status == "published",
                Publication.id != publication.id,
            )
        )
    ).scalar_one_or_none()
    if other is not None:
        logger.error(
            "double_post_detected",
            publication_id=str(publication.id),
            post_id=str(post.id),
            existing_published_id=str(other),
            was_failed_correction=was_failed,
        )
        raise DoublePostDetected()

    publication.status = "published"
    publication.completed_at = now
    publication.error_code = None
    publication.error_category = None
    publication.error_message = None
    publication.retryable = False
    if platform_row is not None:
        publication.external_post_id = platform_row.platform_post_id
        publication.external_url = platform_row.platform_post_url
    await session.flush()

    patch: dict[str, Any] = {
        "published_at": now,
        "zernio_post_id": publication.zernio_post_id,
        "last_error": None,
    }
    updated = post
    if post.status == "publishing":
        updated = await _cas_post(
            session,
            post,
            from_status="publishing",
            to_status="published",
            patch=patch,
        )
    elif post.status == "failed" and was_failed:
        # Late success correcting a failed publication — post may still be failed.
        # failed → scheduled is legal, but published is not. Leave post failed and
        # only correct the publication when ux_pub_published allows (above).
        # If the post is still publishing from a later attempt, do not touch it.
        pass
    elif post.status == "published":
        pass
    else:
        logger.warning(
            "publish_success_unexpected_post_status",
            post_id=str(post.id),
            status=post.status,
            publication_id=str(publication.id),
        )

    await notifications.fan_out(
        session,
        organization_id=updated.organization_id,
        brand_id=updated.brand_id,
        type="post_published",
        message_key="post_published",
        params={"title": _caption_snippet(updated)},
        target_href=f"/posts/{updated.id}",
        actor_user_id=actor_user_id,
        actor_ref=actor_ref,
        recipient_user_ids=[updated.created_by],
    )
    await audit.record(
        session,
        organization_id=updated.organization_id,
        brand_id=updated.brand_id,
        actor_kind="system",
        actor_ref=actor_ref,
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="post.published",
        target_type="post",
        target_id=updated.id,
        meta={"publication_id": str(publication.id)},
    )
    return ApplyPublishResultOutcome(
        publication_status="published",
        post_status=updated.status,
        finalized=True,
        should_retry_job=False,
        retry_after_seconds=None,
        should_auto_retry_publication=False,
        terminal=True,
    )


async def _finalize_terminal_failure(
    session: AsyncSession,
    *,
    publication: Publication,
    post: Post,
    code: str,
    category: PublicationErrorCategory,
    message: str,
    response_payload: dict[str, Any] | None,
    actor_ref: str,
    actor_name: str,
    actor_user_id: uuid.UUID | None,
) -> ApplyPublishResultOutcome:
    if publication.status in ("published", "skipped", "cancelled"):
        return ApplyPublishResultOutcome(
            publication_status=publication.status,
            post_status=None,
            finalized=True,
            should_retry_job=False,
            retry_after_seconds=None,
            should_auto_retry_publication=False,
            error_code=publication.error_code,
            terminal=True,
        )

    now = utc_now()
    publication.status = "failed"
    publication.completed_at = now
    publication.error_code = code
    publication.error_category = category
    publication.error_message = message
    publication.retryable = False
    publication.response_payload = response_payload
    await session.flush()

    error = _last_error(code, message, when=now)
    updated = post
    if post.status == "publishing":
        updated = await _cas_post(
            session,
            post,
            from_status="publishing",
            to_status="failed",
            patch={"last_error": error},
        )
    await _notify_and_audit_failure(
        session,
        post=updated,
        code=code,
        message=message,
        publication_id=publication.id,
        actor_ref=actor_ref,
        actor_name=actor_name,
        actor_user_id=actor_user_id,
    )
    return ApplyPublishResultOutcome(
        publication_status="failed",
        post_status=updated.status,
        finalized=True,
        should_retry_job=False,
        retry_after_seconds=None,
        should_auto_retry_publication=False,
        error_code=code,
        terminal=True,
    )


async def _finalize_retryable_or_terminal(
    session: AsyncSession,
    *,
    publication: Publication,
    post: Post,
    mapped: MappedError,
    message: str,
    retry_after_seconds: float | None,
    response_payload: dict[str, Any] | None,
    actor_ref: str,
    actor_name: str,
    actor_user_id: uuid.UUID | None,
) -> ApplyPublishResultOutcome:
    can_retry = publication.attempt_no < mapped.max_attempts
    if not can_retry:
        return await _finalize_terminal_failure(
            session,
            publication=publication,
            post=post,
            code=mapped.code,
            category=mapped.publication_category,
            message=message,
            response_payload=response_payload,
            actor_ref=actor_ref,
            actor_name=actor_name,
            actor_user_id=actor_user_id,
        )

    now = utc_now()
    publication.status = "failed"
    publication.completed_at = now
    publication.error_code = mapped.code
    publication.error_category = mapped.publication_category
    publication.error_message = message
    publication.retryable = True
    publication.response_payload = response_payload
    await session.flush()

    error = _last_error(mapped.code, message, when=now)
    updated = post
    if post.status == "publishing":
        updated = await _cas_post(
            session,
            post,
            from_status="publishing",
            to_status="failed",
            patch={"last_error": error},
        )
    await _notify_and_audit_failure(
        session,
        post=updated,
        code=mapped.code,
        message=message,
        publication_id=publication.id,
        actor_ref=actor_ref,
        actor_name=actor_name,
        actor_user_id=actor_user_id,
    )

    delay = retry_after_seconds
    if delay is None:
        delay = auto_retry_delay_seconds(publication.attempt_no)

    # Post is failed(retryable); caller transitions failed→scheduled and enqueues.
    return ApplyPublishResultOutcome(
        publication_status="failed",
        post_status="failed",
        finalized=False,
        should_retry_job=False,
        retry_after_seconds=delay,
        should_auto_retry_publication=True,
        error_code=mapped.code,
        terminal=False,
    )


async def _notify_and_audit_failure(
    session: AsyncSession,
    *,
    post: Post,
    code: str,
    message: str,
    publication_id: uuid.UUID | None,
    actor_ref: str = SYSTEM_ACTOR.ref,
    actor_name: str = SYSTEM_ACTOR.name,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    await notifications.fan_out(
        session,
        organization_id=post.organization_id,
        brand_id=post.brand_id,
        type="post_failed",
        message_key="post_failed",
        params={"title": _caption_snippet(post), "reason": message, "code": code},
        target_href=f"/posts/{post.id}",
        actor_user_id=actor_user_id,
        actor_ref=actor_ref,
        recipient_user_ids=[post.created_by],
    )
    meta: dict[str, Any] = {"error_code": code}
    if publication_id is not None:
        meta["publication_id"] = str(publication_id)
    await audit.record(
        session,
        organization_id=post.organization_id,
        brand_id=post.brand_id,
        actor_kind="system",
        actor_ref=actor_ref,
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="post.failed",
        target_type="post",
        target_id=post.id,
        meta=meta,
    )


def _caption_snippet(post: Post) -> str:
    variants = list(post.variants or [])
    ar = next((v for v in variants if v.get("lang") == "ar"), None)
    en = next((v for v in variants if v.get("lang") == "en"), None)
    caption = str((ar or en or {}).get("caption", ""))
    return f"{caption[:60]}…" if len(caption) > 60 else caption


def caption_for_publish(post: Post) -> str:
    """Prefer Arabic, then English, then first variant caption."""
    variants = list(post.variants or [])
    if not variants:
        return ""
    ar = next((v for v in variants if v.get("lang") == "ar"), None)
    en = next((v for v in variants if v.get("lang") == "en"), None)
    chosen = ar or en or variants[0]
    caption = str(chosen.get("caption") or "")
    hashtags = chosen.get("hashtags") or []
    if isinstance(hashtags, list) and hashtags:
        tags = " ".join(str(t) for t in hashtags if t)
        if tags:
            caption = f"{caption}\n{tags}" if caption else tags
    return caption


async def assert_media_publishable(session: AsyncSession, post: Post) -> None:
    """Schedule-time gate: template media without a rendered bitmap cannot publish."""
    media_rows = (
        await session.execute(
            select(PostMedia)
            .where(PostMedia.post_id == post.id)
            .order_by(PostMedia.position.asc())
        )
    ).scalars().all()
    if not media_rows:
        return
    asset_ids = [row.media_asset_id for row in media_rows]
    assets = await media_feature.get_assets_by_ids(
        session,
        organization_id=post.organization_id,
        brand_id=post.brand_id,
        asset_ids=asset_ids,
    )
    by_id = {asset.id: asset for asset in assets}
    for row in media_rows:
        asset = by_id.get(row.media_asset_id)
        if asset is None:
            continue
        if asset.kind == "template":
            from app.core.errors import ApiError

            raise ApiError(
                "MEDIA_NOT_PUBLISHABLE",
                "Template media must be rendered before scheduling or publishing",
                status_code=409,
            )


async def build_media_items_for_publish(
    session: AsyncSession,
    post: Post,
    storage: ObjectStorage,
) -> list[dict[str, Any]]:
    media_rows = (
        await session.execute(
            select(PostMedia)
            .where(PostMedia.post_id == post.id)
            .order_by(PostMedia.position.asc())
        )
    ).scalars().all()
    if not media_rows:
        return []
    assets = await media_feature.get_assets_by_ids(
        session,
        organization_id=post.organization_id,
        brand_id=post.brand_id,
        asset_ids=[row.media_asset_id for row in media_rows],
    )
    by_id = {asset.id: asset for asset in assets}
    items: list[dict[str, Any]] = []
    for row in media_rows:
        asset = by_id.get(row.media_asset_id)
        if asset is None:
            continue
        kind = "video" if asset.kind == "video" else "image"
        items.append({"type": kind, "url": storage.public_url(asset.r2_key)})
    return items


async def build_publish_request(
    session: AsyncSession,
    *,
    post: Post,
    publication: Publication,
    social_account: SocialAccount,
    storage: ObjectStorage,
    correlation_id: str | None = None,
) -> Any:
    """Assemble a PublishRequest for SocialProvider.publish."""
    from app.integrations.social.ports import PublishRequest

    if not social_account.zernio_account_id:
        raise ClaimCheckFailed(
            "Social account is missing provider account id",
            code="ACCOUNT_DISCONNECTED",
        )
    media_items = await build_media_items_for_publish(session, post, storage)
    metadata: dict[str, Any] = {
        "post_id": str(post.id),
        "publication_id": str(publication.id),
        "organization_id": str(post.organization_id),
        "brand_id": str(post.brand_id),
    }
    if correlation_id:
        metadata["correlation_id"] = correlation_id
    return PublishRequest(
        content=caption_for_publish(post),
        platforms=[
            {
                "platform": post.platform,
                "accountId": social_account.zernio_account_id,
            }
        ],
        media_items=media_items,
        metadata=metadata,
        idempotency_key=publication.idempotency_key,
    )


async def apply_webhook_event(
    session: AsyncSession,
    *,
    event_type: str,
    payload: dict[str, Any],
    alias: str | None = None,
) -> None:
    """Map a Zernio ``post.*`` webhook into apply_publish_result (architecture/10 §7)."""
    del alias  # reserved for credential-scoped logging / future pin checks
    if not event_type.startswith("post."):
        return
    if event_type == "post.scheduled":
        return  # never regress

    raw_data = payload.get("data")
    data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else payload
    metadata_raw = data.get("metadata") or payload.get("metadata") or {}
    metadata: dict[str, Any] = (
        dict(metadata_raw) if isinstance(metadata_raw, dict) else {}
    )

    publication = await _resolve_publication_for_webhook(
        session, data=data, metadata=metadata
    )
    if publication is None:
        logger.info(
            "apply_webhook_event_unresolved",
            event_type=event_type,
            zernio_post_id=data.get("id") or data.get("postId"),
        )
        return

    outcome = _webhook_to_publish_result(event_type=event_type, data=data)
    if outcome is None:
        return
    await apply_publish_result(session, publication.id, outcome)


async def _resolve_publication_for_webhook(
    session: AsyncSession,
    *,
    data: dict[str, Any],
    metadata: dict[str, Any],
) -> Publication | None:
    pub_id_raw = metadata.get("publication_id")
    if pub_id_raw:
        try:
            pub_id = uuid.UUID(str(pub_id_raw))
        except ValueError:
            pub_id = None
        if pub_id is not None:
            publication = await repository.get_publication(session, pub_id)
            if publication is not None:
                org_raw = metadata.get("organization_id")
                post_raw = metadata.get("post_id")
                if org_raw and str(publication.organization_id) != str(org_raw):
                    return None
                if post_raw and str(publication.post_id) != str(post_raw):
                    return None
                return publication

    zernio_post_id = str(
        data.get("id") or data.get("postId") or data.get("post_id") or ""
    )
    if zernio_post_id:
        found = await repository.get_by_zernio_post_id(session, zernio_post_id)
        if found is not None:
            return found

    post_raw = metadata.get("post_id")
    if post_raw:
        try:
            post_id = uuid.UUID(str(post_raw))
        except ValueError:
            return None
        return await repository.get_latest_by_post(session, post_id)
    return None


def _webhook_to_publish_result(
    *, event_type: str, data: dict[str, Any]
) -> PublishResult | None:
    zernio_post_id = str(
        data.get("id") or data.get("postId") or data.get("post_id") or ""
    ) or None
    platforms_raw = data.get("platforms")
    platforms: list[PlatformOutcome] = []
    if isinstance(platforms_raw, list):
        for row in platforms_raw:
            if not isinstance(row, dict):
                continue
            platforms.append(
                PlatformOutcome(
                    platform=str(row.get("platform") or ""),
                    account_id=str(row.get("accountId") or row.get("account_id") or ""),
                    status=str(row.get("status") or "publishing"),
                    platform_post_id=(
                        str(row["platformPostId"])
                        if row.get("platformPostId")
                        else None
                    ),
                    platform_post_url=(
                        str(row["platformPostUrl"])
                        if row.get("platformPostUrl")
                        else None
                    ),
                    error_category=(
                        str(row["errorCategory"])
                        if row.get("errorCategory")
                        else None
                    ),
                    error_message=(
                        str(row["errorMessage"]) if row.get("errorMessage") else None
                    ),
                )
            )

    is_failed = event_type == "post.failed" or (
        event_type.startswith("post.platform.") and event_type.endswith(".failed")
    )
    if is_failed:
        return PublishResult(
            kind="rejected",
            zernio_post_id=zernio_post_id,
            platforms=platforms,
            error_category=str(data.get("errorCategory") or "platform_error"),
            error_message=str(data.get("errorMessage") or "Post failed"),
            raw_response=data if isinstance(data, dict) else None,
        )

    if event_type == "post.published" or event_type.startswith("post.platform."):
        if not platforms and zernio_post_id:
            platforms = [
                PlatformOutcome(
                    platform=str(data.get("platform") or "instagram"),
                    account_id=str(data.get("accountId") or ""),
                    status="published",
                    platform_post_id=(
                        str(data["platformPostId"])
                        if data.get("platformPostId")
                        else None
                    ),
                    platform_post_url=(
                        str(data["platformPostUrl"])
                        if data.get("platformPostUrl")
                        else None
                    ),
                )
            ]
        elif platforms:
            platforms = [
                p.model_copy(update={"status": "published"})
                if p.status in ("pending", "publishing")
                else p
                for p in platforms
            ]
        return PublishResult(
            kind="existing",
            zernio_post_id=zernio_post_id,
            platforms=platforms,
            raw_response=data if isinstance(data, dict) else None,
        )

    return None


async def mark_outcome_unknown(
    session: AsyncSession,
    publication_id: uuid.UUID,
) -> ApplyPublishResultOutcome:
    """Terminal OUTCOME_UNKNOWN for reconcile past the 30-minute deadline."""
    publication = await repository.get_publication_for_update(session, publication_id)
    if publication is None:
        raise ClaimCheckFailed(
            f"Publication {publication_id} not found",
            code="NOT_FOUND",
        )
    if publication.status in _TERMINAL_PUBLICATION:
        return ApplyPublishResultOutcome(
            publication_status=publication.status,
            post_status=None,
            finalized=True,
            should_retry_job=False,
            retry_after_seconds=None,
            should_auto_retry_publication=False,
            error_code=publication.error_code,
            terminal=True,
        )
    post = await session.get(Post, publication.post_id)
    if post is None:
        raise ClaimCheckFailed("Post not found for publication", code="NOT_FOUND")
    return await _finalize_terminal_failure(
        session,
        publication=publication,
        post=post,
        code="OUTCOME_UNKNOWN",
        category="network",
        message="Publish outcome unknown after 30 minutes",
        response_payload=None,
        actor_ref=SYSTEM_ACTOR.ref,
        actor_name=SYSTEM_ACTOR.name,
        actor_user_id=None,
    )


__all__ = [
    "SYSTEM_ACTOR",
    "ApplyPublishResultOutcome",
    "MappedError",
    "apply_publish_result",
    "apply_webhook_event",
    "assert_media_publishable",
    "auto_retry_delay_seconds",
    "build_publish_request",
    "caption_for_publish",
    "check_freeze",
    "check_lateness",
    "check_media_reachable",
    "check_velocity",
    "compute_velocity_hold_until",
    "fail_post_at_claim",
    "get_inflight_publication",
    "get_latest_publication",
    "insert_publication_sent",
    "map_error_category",
    "mark_outcome_unknown",
    "mark_post_publishing",
    "next_attempt_no",
    "resolve_social_account_for_post",
    "skip_publication_for_freeze",
]
