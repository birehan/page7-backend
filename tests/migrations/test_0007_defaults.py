"""Server-default smoke tests and trigger backstop for Phase 6 posts tables."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.auth.models import User
from app.features.brands.models import Brand
from app.features.organizations.models import Organization
from app.features.posts.models import Post, PostStatusTransition
from app.features.review_links.models import ReviewLink


async def _seed_org_brand_user(
    db_session: AsyncSession,
) -> tuple[Organization, Brand, User]:
    user = User(email="posts-defaults@example.com", name="Posts User")
    org = Organization(name="Posts Org")
    db_session.add_all([user, org])
    await db_session.flush()
    brand = Brand(
        organization_id=org.id,
        name="Posts Brand",
        industry="retail",
        city="Riyadh",
        guidelines={},
    )
    db_session.add(brand)
    await db_session.flush()
    return org, brand, user


async def test_posts_defaults(db_session: AsyncSession) -> None:
    org, brand, user = await _seed_org_brand_user(db_session)
    post = Post(
        organization_id=org.id,
        brand_id=brand.id,
        platform="instagram",
        scheduled_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        created_by=user.id,
    )
    db_session.add(post)
    await db_session.flush()
    assert post.status == "draft"
    assert post.schedule_epoch == 0
    assert post.version == 1
    assert post.is_paid is False
    assert post.variants == []
    assert post.risk == {"score": 0, "reasons": []}
    assert post.risk_score == Decimal("0")
    assert post.scheduled_date_riyadh.isoformat() == "2026-09-10"


async def test_scheduled_date_riyadh_midnight_boundary(db_session: AsyncSession) -> None:
    """A UTC instant between 00:00–03:00 Riyadh falls on the prior Riyadh day."""
    org, brand, user = await _seed_org_brand_user(db_session)
    # 2026-09-10 01:30 UTC = 2026-09-10 04:30 Riyadh → same day
    # 2026-09-09 22:30 UTC = 2026-09-10 01:30 Riyadh → Riyadh day 2026-09-10
    post = Post(
        organization_id=org.id,
        brand_id=brand.id,
        platform="instagram",
        scheduled_at=datetime(2026, 9, 9, 22, 30, tzinfo=UTC),
        created_by=user.id,
    )
    db_session.add(post)
    await db_session.flush()
    assert post.scheduled_date_riyadh.isoformat() == "2026-09-10"


async def test_seventeen_transitions_seeded(db_session: AsyncSession) -> None:
    rows = (
        await db_session.execute(sa.select(PostStatusTransition))
    ).scalars().all()
    assert len(rows) == 17
    pairs = {(r.from_status, r.to_status) for r in rows}
    assert ("draft", "in_review") in pairs
    assert ("approved", "scheduled") in pairs
    assert ("published", "draft") in pairs


async def test_status_trigger_rejects_illegal_transition(
    db_session: AsyncSession,
) -> None:
    """Database trigger rejects an illegal pair independently of application code."""
    org, brand, user = await _seed_org_brand_user(db_session)
    post = Post(
        organization_id=org.id,
        brand_id=brand.id,
        platform="instagram",
        scheduled_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        created_by=user.id,
    )
    db_session.add(post)
    await db_session.flush()

    with pytest.raises(DBAPIError) as exc_info:
        await db_session.execute(
            sa.text("UPDATE posts SET status = 'published' WHERE id = :id"),
            {"id": post.id},
        )
        await db_session.flush()
    assert "INVALID_TRANSITION" in str(exc_info.value)


async def test_status_trigger_allows_legal_transition(db_session: AsyncSession) -> None:
    org, brand, user = await _seed_org_brand_user(db_session)
    post = Post(
        organization_id=org.id,
        brand_id=brand.id,
        platform="instagram",
        scheduled_at=datetime(2026, 9, 10, 12, 0, tzinfo=UTC),
        created_by=user.id,
        submitted_at=datetime.now(UTC),
    )
    db_session.add(post)
    await db_session.flush()

    await db_session.execute(
        sa.text(
            "UPDATE posts SET status = 'in_review', submitted_at = now() WHERE id = :id"
        ),
        {"id": post.id},
    )
    await db_session.flush()
    await db_session.refresh(post)
    assert post.status == "in_review"


async def test_review_links_defaults(db_session: AsyncSession) -> None:
    org, brand, user = await _seed_org_brand_user(db_session)
    link = ReviewLink(
        organization_id=org.id,
        brand_id=brand.id,
        token_hash="a" * 64,
        locale="ar",
        created_by=user.id,
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    db_session.add(link)
    await db_session.flush()
    assert link.view_count == 0
    assert link.decision is None
    assert link.created_at is not None
