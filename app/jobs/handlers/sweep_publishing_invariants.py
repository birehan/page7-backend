"""sweep_publishing_invariants — heal orphaned scheduled/publishing state."""

from __future__ import annotations

from typing import Any

import structlog
from sqlalchemy import text

from app.core.time import utc_now
from app.db.session import get_session_factory
from app.features.posts import service as posts_service
from app.features.publishing import fail_post_at_claim, get_inflight_publication, repository

log = structlog.get_logger(__name__)


async def handle(payload: dict[str, Any]) -> None:
    del payload
    factory = get_session_factory()
    now = utc_now()

    async with factory() as session:
        # 1) Due scheduled posts with no live publish_post job → re-enqueue.
        due = await repository.list_due_scheduled_posts(session, now=now)
        for post in due:
            has_job = await repository.has_live_publish_job(
                session, post_id=post.id, epoch=post.schedule_epoch
            )
            if has_job:
                continue
            log.warning(
                "sweep.reenqueue_due_scheduled",
                post_id=str(post.id),
                schedule_epoch=post.schedule_epoch,
                scheduled_at=post.scheduled_at.isoformat(),
            )
            await posts_service._enqueue_publish_post(  # noqa: SLF001
                session,
                post,
                epoch=post.schedule_epoch,
                trigger="scheduled",
                priority=10,
                run_at=post.scheduled_at,
            )

        # 2) Live jobs whose post left scheduled/publishing → cancel.
        live_jobs = await repository.list_live_publish_jobs_with_posts(session)
        for job_id, post_id, status in live_jobs:
            if status in ("scheduled", "publishing"):
                continue
            log.warning(
                "sweep.cancel_stale_job",
                job_id=job_id,
                post_id=str(post_id),
                post_status=status,
            )
            await session.execute(
                text(
                    """
                    UPDATE jobs
                    SET state = 'cancelled', finished_at = now(),
                        locked_by = NULL, locked_at = NULL, lease_until = NULL
                    WHERE id = :id AND state IN ('queued', 'running')
                    """
                ),
                {"id": job_id},
            )

        # 3) publishing with neither live publication nor live job → PUBLISH_STATE_LOST.
        publishing = await repository.list_publishing_posts(session)
        for post in publishing:
            inflight = await get_inflight_publication(session, post.id)
            has_job = await repository.has_any_live_publish_job_for_post(
                session, post_id=post.id
            )
            if inflight is not None or has_job:
                continue
            log.error(
                "sweep.publish_state_lost",
                post_id=str(post.id),
                organization_id=str(post.organization_id),
            )
            await fail_post_at_claim(
                session,
                post,
                code="PUBLISH_STATE_LOST",
                message="Publishing state lost — no live publication or job",
            )

        await session.commit()
