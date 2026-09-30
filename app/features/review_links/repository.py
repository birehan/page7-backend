from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.features.review_links.models import ReviewLink, ReviewLinkPost


async def create_review_link(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    token_hash: str,
    locale: str,
    created_by: uuid.UUID,
    expires_at: datetime,
    post_ids: Sequence[uuid.UUID],
) -> ReviewLink:
    link = ReviewLink(
        organization_id=organization_id,
        brand_id=brand_id,
        token_hash=token_hash,
        locale=locale,
        created_by=created_by,
        expires_at=expires_at,
    )
    session.add(link)
    await session.flush()
    for post_id in post_ids:
        session.add(ReviewLinkPost(review_link_id=link.id, post_id=post_id))
    await session.flush()
    return link


async def get_by_token_hash(
    session: AsyncSession, *, token_hash: str
) -> ReviewLink | None:
    stmt = select(ReviewLink).where(ReviewLink.token_hash == token_hash)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_frozen_post_ids(
    session: AsyncSession, *, review_link_id: uuid.UUID
) -> list[uuid.UUID]:
    stmt = select(ReviewLinkPost.post_id).where(
        ReviewLinkPost.review_link_id == review_link_id
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def record_view(session: AsyncSession, *, review_link_id: uuid.UUID) -> None:
    await session.execute(
        update(ReviewLink)
        .where(ReviewLink.id == review_link_id)
        .values(
            view_count=ReviewLink.view_count + 1,
            last_viewed_at=utc_now(),
        )
    )


async def record_decision(
    session: AsyncSession,
    *,
    review_link_id: uuid.UUID,
    decision: str,
    decided_at: datetime,
    decided_by_name: str,
    decision_comment: str | None,
    decided_ip: str | None,
) -> ReviewLink:
    link = await session.get(ReviewLink, review_link_id)
    assert link is not None
    link.decision = decision
    link.decided_at = decided_at
    link.decided_by_name = decided_by_name
    link.decision_comment = decision_comment
    link.decided_ip = decided_ip
    await session.flush()
    return link
