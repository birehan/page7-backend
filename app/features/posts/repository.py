from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.features.posts.models import (
    Post,
    PostComment,
    PostMedia,
    PostStatusTransition,
    PostVersion,
)

_ALLOWED_CAS_PATCH = frozenset(
    {
        "submitted_at",
        "approved_at",
        "approved_by_user_id",
        "approved_via",
        "approved_review_link_id",
        "change_request_reason",
        "reject_reason",
        "last_error",
        "published_at",
        "schedule_epoch",
        "scheduled_at",
        "first_comment",
        "title",
        "internal_note",
        "pillar_id",
        "cultural_event_id",
        "is_paid",
        "variants",
        "risk",
        "risk_score",
        "platform",
        "group_id",
        "ai_decision_id",
        "brand_version",
        "zernio_post_id",
    }
)


async def is_legal_transition(
    session: AsyncSession, *, from_status: str, to_status: str
) -> bool:
    stmt = select(PostStatusTransition).where(
        PostStatusTransition.from_status == from_status,
        PostStatusTransition.to_status == to_status,
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def get_post(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
) -> Post | None:
    stmt = select(Post).where(
        Post.id == post_id,
        Post.brand_id == brand_id,
        Post.organization_id == organization_id,
        Post.deleted_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_posts(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    status: str | None = None,
    platform: str | None = None,
    scheduled_from: datetime | None = None,
    scheduled_to: datetime | None = None,
) -> Sequence[Post]:
    stmt = select(Post).where(
        Post.organization_id == organization_id,
        Post.brand_id == brand_id,
        Post.deleted_at.is_(None),
    )
    if status is not None:
        stmt = stmt.where(Post.status == status)
    if platform is not None:
        stmt = stmt.where(Post.platform == platform)
    if scheduled_from is not None:
        stmt = stmt.where(Post.scheduled_at >= scheduled_from)
    if scheduled_to is not None:
        stmt = stmt.where(Post.scheduled_at < scheduled_to)
    stmt = stmt.order_by(Post.scheduled_at.asc(), Post.id.asc())
    return (await session.execute(stmt)).scalars().all()


async def list_approval_queue(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
) -> Sequence[Post]:
    stmt = (
        select(Post)
        .where(
            Post.organization_id == organization_id,
            Post.brand_id == brand_id,
            Post.status == "in_review",
            Post.deleted_at.is_(None),
        )
        .order_by(Post.risk_score.desc(), Post.submitted_at.asc(), Post.id.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def list_group(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    group_id: uuid.UUID,
) -> Sequence[Post]:
    stmt = (
        select(Post)
        .where(
            Post.organization_id == organization_id,
            Post.brand_id == brand_id,
            Post.group_id == group_id,
            Post.deleted_at.is_(None),
        )
        .order_by(Post.created_at.asc(), Post.id.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def insert_post(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    created_by: uuid.UUID,
    platform: str,
    scheduled_at: datetime,
    variants: list[dict[str, Any]],
    risk: dict[str, Any],
    risk_score: Decimal,
    is_paid: bool = False,
    first_comment: str | None = None,
    pillar_id: uuid.UUID | None = None,
    cultural_event_id: uuid.UUID | None = None,
    group_id: uuid.UUID | None = None,
    ai_decision_id: uuid.UUID | None = None,
    title: str | None = None,
    internal_note: str | None = None,
) -> Post:
    post = Post(
        organization_id=organization_id,
        brand_id=brand_id,
        created_by=created_by,
        platform=platform,
        status="draft",
        scheduled_at=scheduled_at,
        variants=variants,
        risk=risk,
        risk_score=risk_score,
        is_paid=is_paid,
        first_comment=first_comment,
        pillar_id=pillar_id,
        cultural_event_id=cultural_event_id,
        group_id=group_id,
        ai_decision_id=ai_decision_id,
        title=title,
        internal_note=internal_note,
        version=1,
    )
    session.add(post)
    await session.flush()
    return post


async def cas_transition(
    session: AsyncSession,
    *,
    post_id: uuid.UUID,
    brand_id: uuid.UUID,
    from_status: str,
    expected_version: int,
    to_status: str,
    patch: dict[str, Any] | None = None,
) -> Post | None:
    """Compare-and-swap status transition. Returns the updated row, or None."""
    values: dict[str, Any] = {
        "status": to_status,
        "version": Post.version + 1,
        "updated_at": utc_now(),
    }
    if patch:
        for key, value in patch.items():
            if key not in _ALLOWED_CAS_PATCH:
                raise ValueError(f"disallowed CAS patch field: {key}")
            values[key] = value

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


async def cas_update_content(
    session: AsyncSession,
    *,
    post_id: uuid.UUID,
    brand_id: uuid.UUID,
    expected_version: int,
    allowed_statuses: Sequence[str],
    values: dict[str, Any],
) -> Post | None:
    """CAS content edit (no status change). Returns updated row or None."""
    for key in values:
        if key not in _ALLOWED_CAS_PATCH:
            raise ValueError(f"disallowed CAS patch field: {key}")
    stmt = (
        update(Post)
        .where(
            Post.id == post_id,
            Post.brand_id == brand_id,
            Post.version == expected_version,
            Post.status.in_(list(allowed_statuses)),
            Post.deleted_at.is_(None),
        )
        .values(
            **values,
            version=Post.version + 1,
            updated_at=utc_now(),
        )
        .returning(Post)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def replace_post_media(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    items: Sequence[tuple[uuid.UUID, int, str | None, dict[str, Any] | None]],
) -> list[PostMedia]:
    """Replace the ordered post_media set. items = (asset_id, position, alt, crop)."""
    await session.execute(delete(PostMedia).where(PostMedia.post_id == post_id))
    rows: list[PostMedia] = []
    for asset_id, position, alt_override, crop in items:
        row = PostMedia(
            organization_id=organization_id,
            brand_id=brand_id,
            post_id=post_id,
            media_asset_id=asset_id,
            position=position,
            alt_override=alt_override,
            crop=crop,
        )
        session.add(row)
        rows.append(row)
    await session.flush()
    return rows


async def list_post_media(
    session: AsyncSession, *, post_id: uuid.UUID
) -> Sequence[PostMedia]:
    stmt = (
        select(PostMedia)
        .where(PostMedia.post_id == post_id)
        .order_by(PostMedia.position.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def list_post_media_for_posts(
    session: AsyncSession, *, post_ids: Sequence[uuid.UUID]
) -> Sequence[PostMedia]:
    if not post_ids:
        return []
    stmt = (
        select(PostMedia)
        .where(PostMedia.post_id.in_(list(post_ids)))
        .order_by(PostMedia.post_id.asc(), PostMedia.position.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def next_version_number(session: AsyncSession, *, post_id: uuid.UUID) -> int:
    stmt = select(func.coalesce(func.max(PostVersion.version), 0)).where(
        PostVersion.post_id == post_id
    )
    return int((await session.execute(stmt)).scalar_one()) + 1


async def insert_post_version(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    version: int,
    post_row_version: int,
    reason: str,
    author_user_id: uuid.UUID | None,
    author_ref: str,
    author_name: str,
    snapshot: dict[str, Any],
    brand_version: int | None = None,
) -> PostVersion:
    row = PostVersion(
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
        version=version,
        post_row_version=post_row_version,
        reason=reason,
        author_user_id=author_user_id,
        author_ref=author_ref,
        author_name=author_name,
        brand_version=brand_version,
        snapshot=snapshot,
    )
    session.add(row)
    await session.flush()
    return row


async def list_versions(
    session: AsyncSession, *, post_id: uuid.UUID
) -> Sequence[PostVersion]:
    stmt = (
        select(PostVersion)
        .where(PostVersion.post_id == post_id)
        .order_by(PostVersion.version.desc())
    )
    return (await session.execute(stmt)).scalars().all()


async def insert_comment(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    post_id: uuid.UUID,
    author_user_id: uuid.UUID,
    author_name: str,
    body: str,
    lang: str,
) -> PostComment:
    row = PostComment(
        organization_id=organization_id,
        brand_id=brand_id,
        post_id=post_id,
        author_user_id=author_user_id,
        author_name=author_name,
        body=body,
        lang=lang,
    )
    session.add(row)
    await session.flush()
    return row


async def list_comments(
    session: AsyncSession, *, post_id: uuid.UUID
) -> Sequence[PostComment]:
    stmt = (
        select(PostComment)
        .where(PostComment.post_id == post_id)
        .order_by(PostComment.created_at.asc(), PostComment.id.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def get_comment(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    comment_id: uuid.UUID,
) -> PostComment | None:
    stmt = select(PostComment).where(
        PostComment.id == comment_id,
        PostComment.organization_id == organization_id,
        PostComment.brand_id == brand_id,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def resolve_comment(
    session: AsyncSession,
    *,
    comment: PostComment,
    resolved_by: uuid.UUID,
) -> PostComment:
    comment.resolved_at = utc_now()
    comment.resolved_by = resolved_by
    await session.flush()
    return comment


async def soft_delete_brand_posts(
    session: AsyncSession, *, brand_id: uuid.UUID, deleted_by: uuid.UUID
) -> int:
    result = await session.execute(
        update(Post)
        .where(Post.brand_id == brand_id, Post.deleted_at.is_(None))
        .values(deleted_at=utc_now(), deleted_by=deleted_by)
    )
    return int(getattr(result, "rowcount", 0) or 0)


async def media_referenced_by_active_posts(
    session: AsyncSession, *, media_asset_id: uuid.UUID
) -> bool:
    """True when the asset is attached to a post in (scheduled, publishing)."""
    stmt = (
        select(PostMedia.id)
        .join(Post, Post.id == PostMedia.post_id)
        .where(
            PostMedia.media_asset_id == media_asset_id,
            Post.deleted_at.is_(None),
            Post.status.in_(("scheduled", "publishing")),
        )
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None
