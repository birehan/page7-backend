"""publish_post — claim-side tx1, SocialProvider.publish, apply_publish_result tx2.

Architecture/10 §6 exactly-once flow. Payload: post_id, epoch, organization_id,
optional trigger (default scheduled), optional run_at ISO for lateness.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select

from app.core.config import get_settings
from app.core.time import utc_now
from app.db.session import get_session_factory
from app.features.posts import Post
from app.features.publishing import (
    ClaimCheckFailed,
    NonDefinitiveOutcome,
    apply_publish_result,
    build_publish_request,
    check_freeze,
    check_lateness,
    check_media_reachable,
    check_velocity,
    fail_post_at_claim,
    get_inflight_publication,
    insert_publication_sent,
    mark_post_publishing,
    next_attempt_no,
    resolve_social_account_for_post,
    skip_publication_for_freeze,
)
from app.features.publishing.models import Publication
from app.features.social_accounts import get_account, get_credential
from app.integrations.social import get_social_provider_for_credential
from app.integrations.storage import get_object_storage
from app.jobs.errors import RetryableError, TerminalError

log = structlog.get_logger(__name__)

_NON_DEFINITIVE_BACKOFF = (15.0, 45.0, 120.0)


def _parse_run_at(raw: Any, *, fallback: datetime) -> datetime:
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    if isinstance(raw, str) and raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
    return fallback if fallback.tzinfo else fallback.replace(tzinfo=UTC)


async def handle(payload: dict[str, Any]) -> None:
    post_id_raw = payload.get("post_id")
    epoch_raw = payload.get("epoch")
    if not isinstance(post_id_raw, str) or epoch_raw is None:
        raise TerminalError("PUBLISH_POST_BAD_PAYLOAD")
    try:
        post_id = uuid.UUID(post_id_raw)
        epoch = int(epoch_raw)
    except (ValueError, TypeError) as exc:
        raise TerminalError("PUBLISH_POST_BAD_PAYLOAD") from exc

    trigger = str(payload.get("trigger") or "scheduled")
    correlation_id = payload.get("_correlation_id")
    if correlation_id is not None:
        correlation_id = str(correlation_id)

    factory = get_session_factory()
    settings = get_settings()
    storage = get_object_storage(settings)

    publication: Publication | None = None
    social_account_id: uuid.UUID | None = None
    resume = False

    async with factory() as session:
        result = await session.execute(
            select(Post).where(Post.id == post_id).with_for_update()
        )
        post = result.scalar_one_or_none()
        if post is None:
            await session.commit()
            return

        inflight = await get_inflight_publication(session, post.id)
        if (
            post.status == "publishing"
            and inflight is not None
            and inflight.status in ("sent", "accepted")
            and trigger in ("resume", "scheduled", "publish_now", "retry", "auto_retry")
        ):
            # Crash-recovery / resume: skip claim inserts, reuse publication.
            publication = inflight
            social_account_id = inflight.social_account_id
            resume = True
            await session.commit()
        elif post.status != "scheduled" or post.schedule_epoch != epoch:
            await session.commit()
            return
        else:
            # --- tx1 claim checks ---
            if await check_freeze(session, post.organization_id):
                await fail_post_at_claim(
                    session,
                    post,
                    code="FROZEN",
                    message="Publishing is frozen for this organization",
                )
                await session.commit()
                return

            run_at = _parse_run_at(payload.get("run_at"), fallback=post.scheduled_at)
            if check_lateness(run_at, now=utc_now()):
                await fail_post_at_claim(
                    session,
                    post,
                    code="MISSED_SCHEDULE",
                    message="Publish job claimed past the lateness window",
                )
                await session.commit()
                return

            try:
                account = await resolve_social_account_for_post(session, post)
            except ClaimCheckFailed as exc:
                await fail_post_at_claim(
                    session, post, code=exc.code, message=str(exc)
                )
                await session.commit()
                return

            try:
                await check_media_reachable(session, post, storage)
            except ClaimCheckFailed as exc:
                await fail_post_at_claim(
                    session, post, code=exc.code, message=str(exc)
                )
                await session.commit()
                return

            hold_until = await check_velocity(
                session, account.id, post.platform, now=utc_now()
            )
            if hold_until is not None:
                await session.commit()
                delay = max(1.0, (hold_until - utc_now()).total_seconds())
                log.info(
                    "publish_post.velocity_hold",
                    post_id=str(post.id),
                    hold_until=hold_until.isoformat(),
                    delay_seconds=delay,
                )
                raise RetryableError(after=delay)

            attempt_no = await next_attempt_no(session, post.id)
            publication = await insert_publication_sent(
                session,
                post=post,
                social_account=account,
                attempt_no=attempt_no,
                trigger=trigger,
            )
            await mark_post_publishing(session, post)
            social_account_id = account.id
            await session.commit()

    assert publication is not None
    assert social_account_id is not None

    # Second freeze read after commit (architecture/10 §5).
    async with factory() as session:
        if await check_freeze(session, publication.organization_id):
            post = await session.get(Post, post_id)
            pub = await session.get(Publication, publication.id)
            if post is not None and pub is not None:
                await skip_publication_for_freeze(
                    session, publication=pub, post=post
                )
                await session.commit()
            return

    # Build request + call provider (no DB transaction held across HTTP).
    from app.integrations.social.ports import PublishRequest

    request: PublishRequest
    async with factory() as session:
        post_row = await session.get(Post, post_id)
        pub = await session.get(Publication, publication.id)
        social_account = await get_account(
            session,
            organization_id=publication.organization_id,
            brand_id=publication.brand_id,
            account_id=social_account_id,
        )
        if post_row is None or pub is None or social_account is None:
            return
        if social_account.credential_id is None:
            from app.integrations.social.ports import PublishResult

            await apply_publish_result(
                session,
                pub.id,
                PublishResult(
                    kind="auth_failed",
                    error_message="Provider credential missing at publish time",
                ),
            )
            await session.commit()
            return

        credential = await get_credential(
            session, credential_id=social_account.credential_id
        )
        if credential is None:
            from app.integrations.social.ports import PublishResult

            await apply_publish_result(
                session,
                pub.id,
                PublishResult(
                    kind="auth_failed",
                    error_message="Provider credential missing at publish time",
                ),
            )
            await session.commit()
            return

        if resume and pub.request_payload:
            request = PublishRequest.model_validate(pub.request_payload)
        else:
            request = await build_publish_request(
                session,
                post=post_row,
                publication=pub,
                social_account=social_account,
                storage=storage,
                correlation_id=correlation_id,
            )

        provider = get_social_provider_for_credential(
            settings, alias=credential.alias, secret_ref=credential.secret_ref
        )
        from app.features.publishing.media_staging import (
            ensure_media_urls_provider_fetchable,
        )

        request = await ensure_media_urls_provider_fetchable(
            request,
            storage=storage,
            provider=provider,
            public_base_url=settings.storage.public_base_url,
        )
        pub.request_payload = request.model_dump(mode="json")
        await session.flush()
        await session.commit()

    publish_result = await provider.publish(request)

    async with factory() as session:
        try:
            outcome = await apply_publish_result(
                session, publication.id, publish_result
            )
        except NonDefinitiveOutcome as exc:
            await session.commit()
            raise RetryableError(after=exc.retry_after_seconds) from exc

        if outcome.should_retry_job:
            delay = outcome.retry_after_seconds or 15.0
            await session.commit()
            raise RetryableError(after=delay)

        if outcome.should_auto_retry_publication:
            from app.features.posts import service as posts_service

            post_row = await session.get(Post, post_id)
            if post_row is not None and post_row.status == "failed":
                delay = outcome.retry_after_seconds or 60.0
                run_at = utc_now() + timedelta(seconds=delay)
                actor = posts_service.Actor(
                    name="Publishing pipeline",
                    ref="system:publishing",
                    kind="system",
                    user_id=None,
                )
                await posts_service._enter_scheduled(  # noqa: SLF001
                    session,
                    post_row,
                    actor,
                    storage=storage,
                    audit_action="post.auto_retry_scheduled",
                    trigger="auto_retry",
                    priority=5,
                    run_at=run_at,
                    scheduled_at=run_at,
                )
            await session.commit()
            return

        await session.commit()


def non_definitive_backoff_seconds(job_attempt: int) -> float:
    idx = max(0, min(job_attempt - 1, len(_NON_DEFINITIVE_BACKOFF) - 1))
    return _NON_DEFINITIVE_BACKOFF[idx]
