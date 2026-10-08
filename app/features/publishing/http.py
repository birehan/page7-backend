"""HTTP-facing publishing actions — publish_now, retry_publish, publishing-queue."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ApiError
from app.core.time import utc_now
from app.db.session import get_session_factory
from app.features.posts import Actor, Post
from app.features.posts import service as posts_service
from app.features.publishing import repository
from app.features.publishing.exceptions import ClaimCheckFailed, NonDefinitiveOutcome
from app.features.publishing.schemas import QUEUE_STATUSES, PublishingQueueOut
from app.features.publishing.service import (
    apply_publish_result,
    check_freeze,
    get_latest_publication,
    resolve_social_account_for_post,
)
from app.features.social_accounts import get_account, get_credential
from app.integrations.social import get_social_provider_for_credential
from app.integrations.social.ports import PublishRequest
from app.integrations.storage.ports import ObjectStorage
from app.jobs import queue as job_queue
from app.jobs.errors import RetryableError, TerminalError
from app.jobs.handlers.publish_post import handle as handle_publish_post

# Callers: publish_now / retry_publish. User: post now taking forever + error
# while actually posted. Wait briefly for scheduled→publishing handoff only —
# never hold for terminal published (Instagram finishes via webhook).
_POLL_INTERVAL_S = 0.25
_POLL_DEADLINE_S = 8.0
_INLINE_WORKER_ID = "api-inline"


def _stable_publish_key(post_id: uuid.UUID) -> str:
    return f"publish:{post_id}"


def _stable_retry_key(post_id: uuid.UUID) -> str:
    return f"retry:{post_id}"


def _publish_job_unique_key(post_id: uuid.UUID, epoch: int) -> str:
    return f"publish:{post_id}:{epoch}"

async def _load_post(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
) -> Post:
    await posts_service._assert_brand(  # noqa: SLF001
        session, organization_id=organization_id, brand_id=brand_id
    )
    post = await session.get(Post, post_id)
    if (
        post is None
        or post.organization_id != organization_id
        or post.brand_id != brand_id
        or post.deleted_at is not None
    ):
        raise ApiError("NOT_FOUND", "Post not found", status_code=404)
    return post


async def _require_connected_channel(session: AsyncSession, post: Post) -> None:
    try:
        await resolve_social_account_for_post(session, post)
    except ClaimCheckFailed as exc:
        code = (
            "CHANNEL_NOT_CONNECTED"
            if exc.code in ("ACCOUNT_DISCONNECTED", "CHANNEL_NOT_CONNECTED")
            else exc.code
        )
        raise ApiError(code, str(exc), status_code=409) from exc


async def _bounded_poll_post(
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    storage: ObjectStorage,
) -> Any:
    deadline = asyncio.get_running_loop().time() + _POLL_DEADLINE_S
    factory = get_session_factory()
    while True:
        async with factory() as session:
            out = await posts_service.get_post(
                session,
                organization_id=organization_id,
                brand_id=brand_id,
                post_id=post_id,
                storage=storage,
            )
            await session.commit()
        if out is None:
            raise ApiError("NOT_FOUND", "Post not found", status_code=404)
        # publishing is a successful handoff (Zernio accepted / IG processing).
        # Do not hold the HTTP request until terminal published/failed.
        if out.status != "scheduled":
            return out
        if asyncio.get_running_loop().time() >= deadline:
            return out
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _claim_queued_job_for_inline(
    session: AsyncSession, *, unique_key: str
) -> int | None:
    """Steal a queued publish job so the worker cannot run it in parallel.

    Returns the job id when claimed, or None when the worker already owns it
    (or the row is gone). Without this, publish_now both enqueues and runs
    inline — the worker resume path then calls provider.publish a second time.
    """
    result = await session.execute(
        sa.text(
            """
            UPDATE jobs
            SET
                state       = 'running',
                locked_by   = :worker_id,
                locked_at   = now(),
                started_at  = now(),
                attempts    = attempts + 1,
                lease_until = now() + make_interval(secs => timeout_seconds + 30)
            WHERE unique_key = :key
              AND state = 'queued'
            RETURNING id
            """
        ),
        {"key": unique_key, "worker_id": _INLINE_WORKER_ID},
    )
    row = result.fetchone()
    return int(row[0]) if row else None


async def _run_publish_job_inline(
    payload: dict[str, Any], *, unique_key: str | None = None
) -> None:
    """Run publish_post in-process after claiming the matching queued job.

    If ``unique_key`` is set and the claim fails, skip the inline call — the
    worker already holds the job and will publish once.
    """
    factory = get_session_factory()
    job_id: int | None = None
    if unique_key is not None:
        async with factory() as session:
            job_id = await _claim_queued_job_for_inline(session, unique_key=unique_key)
            await session.commit()
        if job_id is None:
            return

    try:
        await handle_publish_post(payload)
    except RetryableError:
        if job_id is not None:
            async with factory() as session:
                await job_queue.release_to_queued(session, ids=[job_id])
        return
    except TerminalError as exc:
        if job_id is not None:
            async with factory() as session:
                await job_queue.mark_dead(session, job_id=job_id, error=str(exc))
        raise
    except Exception as exc:
        if job_id is not None:
            async with factory() as session:
                await job_queue.mark_failed(
                    session,
                    job_id=job_id,
                    queue="publishing",
                    attempts=1,
                    max_attempts=1,
                    error=str(exc),
                )
        raise

    if job_id is not None:
        async with factory() as session:
            await job_queue.mark_succeeded(session, job_id=job_id)


async def _resume_publication_http(
    session: AsyncSession, publication_id: uuid.UUID
) -> None:
    """Re-issue the same provider call for OUTCOME_UNKNOWN."""
    pub = await repository.get_publication(session, publication_id)
    if pub is None:
        return
    if pub.status == "failed" and pub.error_code == "OUTCOME_UNKNOWN":
        pub.status = "sent"
        pub.completed_at = None
        pub.error_code = None
        pub.error_category = None
        pub.error_message = None
        pub.retryable = None
        await session.flush()
    if not pub.request_payload:
        return
    account = await get_account(
        session,
        organization_id=pub.organization_id,
        brand_id=pub.brand_id,
        account_id=pub.social_account_id,
    )
    if account is None or account.credential_id is None:
        return
    credential = await get_credential(session, credential_id=account.credential_id)
    if credential is None:
        return
    request = PublishRequest.model_validate(pub.request_payload)
    settings = get_settings()
    provider = get_social_provider_for_credential(
        settings, alias=credential.alias, secret_ref=credential.secret_ref
    )
    from app.features.publishing.media_staging import ensure_media_urls_provider_fetchable
    from app.integrations.storage import get_object_storage

    storage = get_object_storage(settings)
    request = await ensure_media_urls_provider_fetchable(
        request,
        storage=storage,
        provider=provider,
        public_base_url=settings.storage.public_base_url,
    )
    pub.request_payload = request.model_dump(mode="json")
    await session.flush()
    await session.commit()
    result = await provider.publish(request)
    factory = get_session_factory()
    async with factory() as apply_session:
        try:
            await apply_publish_result(apply_session, publication_id, result)
            await apply_session.commit()
        except NonDefinitiveOutcome:
            await apply_session.commit()


async def _reconcile_publication_with_provider(
    session: AsyncSession,
    *,
    publication_id: uuid.UUID,
) -> None:
    """Refresh outcome from Zernio for an existing provider post — never re-publish."""
    pub = await repository.get_publication(session, publication_id)
    if pub is None or not pub.zernio_post_id:
        return
    if pub.credential_id is None:
        return
    credential = await get_credential(session, credential_id=pub.credential_id)
    if credential is None:
        return
    settings = get_settings()
    provider = get_social_provider_for_credential(
        settings, alias=credential.alias, secret_ref=credential.secret_ref
    )
    zernio_post_id = pub.zernio_post_id
    # Revive a false-failed publication so apply_publish_result can correct it.
    if pub.status == "failed":
        pub.status = "accepted"
        pub.completed_at = None
        pub.error_code = None
        pub.error_category = None
        pub.error_message = None
        pub.retryable = None
        await session.flush()
    await session.commit()

    result = await provider.get_post(zernio_post_id)
    factory = get_session_factory()
    async with factory() as apply_session:
        # Ensure post can move publishing→published if still failed.
        from app.features.posts import Post as PostModel
        from app.features.publishing.service import _cas_post

        pub2 = await repository.get_publication(apply_session, publication_id)
        if pub2 is None:
            return
        post = await apply_session.get(PostModel, pub2.post_id)
        if post is not None and post.status == "failed":
            try:
                post = await _cas_post(
                    apply_session, post, from_status="failed", to_status="scheduled"
                )
                post = await _cas_post(
                    apply_session,
                    post,
                    from_status="scheduled",
                    to_status="publishing",
                )
            except ClaimCheckFailed:
                pass
        try:
            await apply_publish_result(apply_session, publication_id, result)
            await apply_session.commit()
        except NonDefinitiveOutcome:
            await apply_session.commit()


async def publish_now(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    actor: Actor,
    storage: ObjectStorage,
    idempotency_key: str | None = None,
) -> Any:
    post = await _load_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )

    if await check_freeze(session, organization_id):
        raise ApiError(
            "FROZEN",
            "Publishing is frozen for this organization.",
            status_code=409,
        )

    if post.status not in ("approved", "scheduled"):
        if idempotency_key == _stable_publish_key(post_id):
            return await posts_service.post_to_out(session, post, storage=storage)
        raise ApiError(
            "INVALID_TRANSITION",
            f"Cannot publish a post in status {post.status}",
            status_code=409,
        )

    # If a prior attempt already created a Zernio post, reconcile instead of a new one.
    latest_for_publish = await get_latest_publication(session, post.id)
    if latest_for_publish is not None and latest_for_publish.zernio_post_id:
        await _reconcile_publication_with_provider(
            session, publication_id=latest_for_publish.id
        )
        return await _bounded_poll_post(
            organization_id=organization_id,
            brand_id=brand_id,
            post_id=post_id,
            storage=storage,
        )

    await _require_connected_channel(session, post)

    now = utc_now()
    if post.status == "approved":
        scheduled = await posts_service._enter_scheduled(  # noqa: SLF001
            session,
            post,
            actor,
            storage=storage,
            audit_action="post.publish_now",
            trigger="publish_now",
            priority=5,
            run_at=now,
            scheduled_at=now,
        )
    else:
        old_epoch = post.schedule_epoch
        new_epoch = old_epoch + 1
        await posts_service._cancel_publish_job(  # noqa: SLF001
            session, post_id=post.id, epoch=old_epoch
        )
        post.scheduled_at = now
        post.schedule_epoch = new_epoch
        post.version += 1
        await session.flush()
        await posts_service._enqueue_publish_post(  # noqa: SLF001
            session,
            post,
            epoch=new_epoch,
            trigger="publish_now",
            priority=5,
            run_at=now,
        )
        scheduled = post
    payload = {
        "post_id": str(scheduled.id),
        "epoch": scheduled.schedule_epoch,
        "organization_id": str(scheduled.organization_id),
        "trigger": "publish_now",
        "run_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    unique_key = _publish_job_unique_key(scheduled.id, scheduled.schedule_epoch)
    await session.commit()
    await _run_publish_job_inline(payload, unique_key=unique_key)
    return await _bounded_poll_post(
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
        storage=storage,
    )


async def retry_publish(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    actor: Actor,
    storage: ObjectStorage,
    idempotency_key: str | None = None,
) -> Any:
    post = await _load_post(
        session,
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
    )

    if await check_freeze(session, organization_id):
        raise ApiError(
            "FROZEN",
            "Publishing is frozen for this organization.",
            status_code=409,
        )

    latest = await get_latest_publication(session, post.id)
    now = utc_now()

    if latest is not None and latest.status in ("sent", "accepted"):
        payload = {
            "post_id": str(post.id),
            "epoch": post.schedule_epoch,
            "organization_id": str(post.organization_id),
            "trigger": "resume",
            "run_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        }
        # Resume has no fresh enqueue; run inline only (handler must not double-publish).
        await session.commit()
        await _run_publish_job_inline(payload)
        return await _bounded_poll_post(
            organization_id=organization_id,
            brand_id=brand_id,
            post_id=post_id,
            storage=storage,
        )

    if latest is not None and latest.error_code == "OUTCOME_UNKNOWN":
        await _resume_publication_http(session, latest.id)
        return await _bounded_poll_post(
            organization_id=organization_id,
            brand_id=brand_id,
            post_id=post_id,
            storage=storage,
        )

    # Already created a provider post — reconcile status, do not publish again.
    if latest is not None and latest.zernio_post_id:
        await _reconcile_publication_with_provider(session, publication_id=latest.id)
        return await _bounded_poll_post(
            organization_id=organization_id,
            brand_id=brand_id,
            post_id=post_id,
            storage=storage,
        )

    await _require_connected_channel(session, post)

    if post.status != "failed":
        if idempotency_key == _stable_retry_key(post_id):
            return await posts_service.post_to_out(session, post, storage=storage)
        raise ApiError(
            "INVALID_TRANSITION",
            f"Cannot retry-publish a post in status {post.status}",
            status_code=409,
        )

    scheduled = await posts_service._enter_scheduled(  # noqa: SLF001
        session,
        post,
        actor,
        storage=storage,
        audit_action="post.retry_publish",
        trigger="retry",
        priority=5,
        run_at=now,
        scheduled_at=now,
    )
    payload = {
        "post_id": str(scheduled.id),
        "epoch": scheduled.schedule_epoch,
        "organization_id": str(scheduled.organization_id),
        "trigger": "retry",
        "run_at": now.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    unique_key = _publish_job_unique_key(scheduled.id, scheduled.schedule_epoch)
    await session.commit()
    await _run_publish_job_inline(payload, unique_key=unique_key)
    return await _bounded_poll_post(
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
        storage=storage,
    )


async def get_publishing_queue(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    storage: ObjectStorage,
) -> PublishingQueueOut:
    await posts_service._assert_brand(  # noqa: SLF001
        session, organization_id=organization_id, brand_id=brand_id
    )
    grouped = await repository.list_posts_for_publishing_queue(
        session, organization_id=organization_id, brand_id=brand_id
    )
    out: dict[str, list[Any]] = {status: [] for status in QUEUE_STATUSES}
    for status, posts in grouped.items():
        for post in posts:
            out[status].append(
                await posts_service.post_to_out(session, post, storage=storage)
            )
    return PublishingQueueOut(
        approved=out["approved"],
        scheduled=out["scheduled"],
        publishing=out["publishing"],
        published=out["published"],
        failed=out["failed"],
    )
