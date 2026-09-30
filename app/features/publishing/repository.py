"""Persistence for the publications ledger."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.features.posts import Post
from app.features.publishing.models import Publication

_INFLIGHT = ("pending", "sent", "accepted")
_QUEUE_STATUSES = ("approved", "scheduled", "publishing", "published", "failed")


async def get_publication_for_update(
    session: AsyncSession, publication_id: uuid.UUID
) -> Publication | None:
    stmt = (
        select(Publication)
        .where(Publication.id == publication_id)
        .with_for_update()
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_publication(
    session: AsyncSession, publication_id: uuid.UUID
) -> Publication | None:
    return await session.get(Publication, publication_id)


async def get_inflight_by_post(
    session: AsyncSession, post_id: uuid.UUID
) -> Publication | None:
    stmt = select(Publication).where(
        Publication.post_id == post_id,
        Publication.status.in_(_INFLIGHT),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_latest_by_post(
    session: AsyncSession, post_id: uuid.UUID
) -> Publication | None:
    stmt = (
        select(Publication)
        .where(Publication.post_id == post_id)
        .order_by(Publication.attempt_no.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_by_zernio_post_id(
    session: AsyncSession, zernio_post_id: str
) -> Publication | None:
    stmt = select(Publication).where(Publication.zernio_post_id == zernio_post_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def next_attempt_no(session: AsyncSession, post_id: uuid.UUID) -> int:
    stmt = select(func.coalesce(func.max(Publication.attempt_no), 0)).where(
        Publication.post_id == post_id
    )
    current = (await session.execute(stmt)).scalar_one()
    return int(current) + 1


async def insert_publication(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    social_account_id: uuid.UUID,
    zernio_profile_id: uuid.UUID,
    credential_id: uuid.UUID,
    attempt_no: int,
    trigger: str,
    idempotency_key: str,
    scheduled_for: datetime,
    job_id: int | None = None,
    status: str = "sent",
    request_payload: dict[str, Any] | None = None,
) -> Publication:
    now = utc_now()
    row = Publication(
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
        social_account_id=social_account_id,
        zernio_profile_id=zernio_profile_id,
        credential_id=credential_id,
        attempt_no=attempt_no,
        trigger=trigger,
        idempotency_key=idempotency_key,
        job_id=job_id,
        status=status,
        scheduled_for=scheduled_for,
        sent_at=now if status == "sent" else None,
        request_payload=request_payload,
    )
    session.add(row)
    await session.flush()
    return row


async def count_published_in_window(
    session: AsyncSession,
    *,
    social_account_id: uuid.UUID,
    since: datetime,
) -> int:
    stmt = select(func.count()).select_from(Publication).where(
        Publication.social_account_id == social_account_id,
        Publication.status == "published",
        Publication.completed_at.is_not(None),
        Publication.completed_at > since,
    )
    return int((await session.execute(stmt)).scalar_one())


async def oldest_published_completed_at(
    session: AsyncSession,
    *,
    social_account_id: uuid.UUID,
    since: datetime,
) -> datetime | None:
    stmt = select(func.min(Publication.completed_at)).where(
        Publication.social_account_id == social_account_id,
        Publication.status == "published",
        Publication.completed_at.is_not(None),
        Publication.completed_at > since,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_posts_for_publishing_queue(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
) -> dict[str, list[Post]]:
    """Posts grouped by status for GET .../publishing-queue (all five keys)."""
    stmt: Select[tuple[Post]] = (
        select(Post)
        .where(
            Post.organization_id == organization_id,
            Post.brand_id == brand_id,
            Post.deleted_at.is_(None),
            Post.status.in_(_QUEUE_STATUSES),
        )
        .order_by(Post.scheduled_at.asc())
    )
    rows: Sequence[Post] = (await session.execute(stmt)).scalars().all()
    grouped: dict[str, list[Post]] = {status: [] for status in _QUEUE_STATUSES}
    for post in rows:
        if post.status in grouped:
            grouped[post.status].append(post)
    return grouped


async def list_sent_needing_resume(
    session: AsyncSession, *, older_than: datetime
) -> Sequence[Publication]:
    """sent publications with sent_at older than threshold."""
    stmt = select(Publication).where(
        Publication.status == "sent",
        Publication.sent_at.is_not(None),
        Publication.sent_at < older_than,
    )
    return (await session.execute(stmt)).scalars().all()


async def list_accepted_needing_poll(
    session: AsyncSession, *, older_than: datetime
) -> Sequence[Publication]:
    stmt = select(Publication).where(
        Publication.status == "accepted",
        Publication.sent_at.is_not(None),
        Publication.sent_at < older_than,
        Publication.zernio_post_id.is_not(None),
    )
    return (await session.execute(stmt)).scalars().all()


async def list_unresolved_past_deadline(
    session: AsyncSession, *, older_than: datetime
) -> Sequence[Publication]:
    """Inflight publications past the OUTCOME_UNKNOWN deadline."""
    stmt = select(Publication).where(
        Publication.status.in_(("sent", "accepted")),
        Publication.sent_at.is_not(None),
        Publication.sent_at < older_than,
    )
    return (await session.execute(stmt)).scalars().all()


async def has_live_publish_job(
    session: AsyncSession, *, post_id: uuid.UUID, epoch: int
) -> bool:
    from sqlalchemy import text

    row = (
        await session.execute(
            text(
                """
                SELECT 1 FROM jobs
                WHERE unique_key = :key
                  AND state IN ('queued', 'running')
                LIMIT 1
                """
            ),
            {"key": f"publish:{post_id}:{epoch}"},
        )
    ).first()
    return row is not None


async def has_any_live_publish_job_for_post(
    session: AsyncSession, *, post_id: uuid.UUID
) -> bool:
    from sqlalchemy import text

    row = (
        await session.execute(
            text(
                """
                SELECT 1 FROM jobs
                WHERE type = 'publish_post'
                  AND payload->>'post_id' = :post_id
                  AND state IN ('queued', 'running')
                LIMIT 1
                """
            ),
            {"post_id": str(post_id)},
        )
    ).first()
    return row is not None


async def cancel_live_publish_jobs_for_post(
    session: AsyncSession, *, post_id: uuid.UUID
) -> int:
    from sqlalchemy import text

    result = await session.execute(
        text(
            """
            UPDATE jobs
            SET state = 'cancelled', finished_at = now(),
                locked_by = NULL, locked_at = NULL, lease_until = NULL
            WHERE type = 'publish_post'
              AND payload->>'post_id' = :post_id
              AND state IN ('queued', 'running')
            """
        ),
        {"post_id": str(post_id)},
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def list_due_scheduled_posts(
    session: AsyncSession, *, now: datetime
) -> Sequence[Post]:
    stmt = select(Post).where(
        Post.status == "scheduled",
        Post.deleted_at.is_(None),
        Post.scheduled_at <= now,
    )
    return (await session.execute(stmt)).scalars().all()


async def list_publishing_posts(session: AsyncSession) -> Sequence[Post]:
    stmt = select(Post).where(
        Post.status == "publishing",
        Post.deleted_at.is_(None),
    )
    return (await session.execute(stmt)).scalars().all()


async def list_live_publish_jobs_with_posts(
    session: AsyncSession,
) -> Sequence[tuple[int, uuid.UUID, str]]:
    """Return (job_id, post_id, post.status) for live publish_post jobs."""
    from sqlalchemy import text

    rows = (
        await session.execute(
            text(
                """
                SELECT j.id, (j.payload->>'post_id')::uuid AS post_id, p.status
                FROM jobs j
                JOIN posts p ON p.id = (j.payload->>'post_id')::uuid
                WHERE j.type = 'publish_post'
                  AND j.state IN ('queued', 'running')
                """
            )
        )
    ).all()
    out: list[tuple[int, uuid.UUID, str]] = []
    for row in rows:
        out.append((int(row.id), uuid.UUID(str(row.post_id)), str(row.status)))
    return out


async def cas_post_status(
    session: AsyncSession,
    *,
    post_id: uuid.UUID,
    brand_id: uuid.UUID,
    from_status: str,
    expected_version: int,
    to_status: str,
    patch: dict[str, Any] | None = None,
) -> Post | None:
    """CAS status update that bypasses posts.service.transition freeze checks.

    Still subject to the DB ``post_status_transitions`` trigger.
    """
    values: dict[str, Any] = {
        "status": to_status,
        "version": Post.version + 1,
        "updated_at": utc_now(),
    }
    if patch:
        values.update(patch)
    stmt = (
        update(Post)
        .where(
            Post.id == post_id,
            Post.brand_id == brand_id,
            Post.status == from_status,
            Post.version == expected_version,
            Post.deleted_at.is_(None),
        )
        .values(**values)
        .returning(Post)
    )
    return (await session.execute(stmt)).scalar_one_or_none()
