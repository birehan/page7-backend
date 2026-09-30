"""Server-default smoke tests for Phase 10 publications table."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.auth.models import User
from app.features.brands.models import Brand
from app.features.organizations.models import Organization
from app.features.posts.models import Post
from app.features.publishing.models import Publication
from app.features.social_accounts.models import (
    SocialAccount,
    ZernioCredential,
    ZernioProfile,
)


async def _seed(
    db_session: AsyncSession,
) -> tuple[Organization, Brand, User, Post, SocialAccount, ZernioProfile, ZernioCredential]:
    user = User(email="pub-defaults@example.com", name="Pub User")
    org = Organization(name="Pub Org")
    db_session.add_all([user, org])
    await db_session.flush()
    brand = Brand(
        organization_id=org.id,
        name="Pub Brand",
        industry="retail",
        city="Riyadh",
        website="https://example.sa",
        guidelines={},
    )
    db_session.add(brand)
    await db_session.flush()

    cred = (
        await db_session.execute(
            select(ZernioCredential).where(ZernioCredential.alias == "t1")
        )
    ).scalar_one()
    profile = ZernioProfile(
        organization_id=org.id,
        brand_id=brand.id,
        credential_id=cred.id,
        zernio_profile_id="zp_pub_defaults",
    )
    db_session.add(profile)
    await db_session.flush()

    account = SocialAccount(
        organization_id=org.id,
        brand_id=brand.id,
        platform="instagram",
        zernio_profile_id=profile.id,
        credential_id=cred.id,
        zernio_account_id="za_pub_defaults",
        status="connected",
    )
    db_session.add(account)
    await db_session.flush()

    post = Post(
        organization_id=org.id,
        brand_id=brand.id,
        platform="instagram",
        status="draft",
        scheduled_at=datetime.now(UTC),
        created_by=user.id,
        variants=[{"lang": "en", "caption": "hello"}],
    )
    db_session.add(post)
    await db_session.flush()
    return org, brand, user, post, account, profile, cred


async def test_publications_defaults(db_session: AsyncSession) -> None:
    org, brand, _user, post, account, profile, cred = await _seed(db_session)
    row = Publication(
        organization_id=org.id,
        brand_id=brand.id,
        post_id=post.id,
        social_account_id=account.id,
        zernio_profile_id=profile.id,
        credential_id=cred.id,
        attempt_no=1,
        trigger="scheduled",
        idempotency_key=f"pub:{post.id}:1",
        scheduled_for=post.scheduled_at,
    )
    db_session.add(row)
    await db_session.flush()
    assert row.status == "pending"
    assert row.id is not None
    assert row.created_at is not None
    assert row.updated_at is not None
    assert row.zernio_post_id is None
    assert row.retryable is None


async def test_publications_inflight_unique(db_session: AsyncSession) -> None:
    org, brand, _user, post, account, profile, cred = await _seed(db_session)
    first = Publication(
        organization_id=org.id,
        brand_id=brand.id,
        post_id=post.id,
        social_account_id=account.id,
        zernio_profile_id=profile.id,
        credential_id=cred.id,
        attempt_no=1,
        trigger="scheduled",
        idempotency_key=f"pub:{post.id}:1",
        status="sent",
        scheduled_for=post.scheduled_at,
    )
    db_session.add(first)
    await db_session.flush()

    second = Publication(
        organization_id=org.id,
        brand_id=brand.id,
        post_id=post.id,
        social_account_id=account.id,
        zernio_profile_id=profile.id,
        credential_id=cred.id,
        attempt_no=2,
        trigger="retry",
        idempotency_key=f"pub:{post.id}:2",
        status="accepted",
        scheduled_for=post.scheduled_at,
    )
    db_session.add(second)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_publications_accepted_and_skipped_status_allowed(
    db_session: AsyncSession,
) -> None:
    org, brand, _user, post, account, profile, cred = await _seed(db_session)
    for status, attempt in (("accepted", 1), ("skipped", 2)):
        row = Publication(
            organization_id=org.id,
            brand_id=brand.id,
            post_id=post.id,
            social_account_id=account.id,
            zernio_profile_id=profile.id,
            credential_id=cred.id,
            attempt_no=attempt,
            trigger="publish_now" if attempt == 1 else "scheduled",
            idempotency_key=f"pub:{post.id}:{attempt}:{status}",
            status=status,
            scheduled_for=post.scheduled_at,
        )
        # accepted is inflight — only one at a time; skipped is terminal.
        if status == "accepted":
            db_session.add(row)
            await db_session.flush()
            row.status = "failed"
            await db_session.flush()
        else:
            db_session.add(row)
            await db_session.flush()


async def test_publications_post_restrict(db_session: AsyncSession) -> None:
    org, brand, _user, post, account, profile, cred = await _seed(db_session)
    row = Publication(
        organization_id=org.id,
        brand_id=brand.id,
        post_id=post.id,
        social_account_id=account.id,
        zernio_profile_id=profile.id,
        credential_id=cred.id,
        attempt_no=1,
        trigger="scheduled",
        idempotency_key=f"pub:{post.id}:1",
        scheduled_for=post.scheduled_at,
    )
    db_session.add(row)
    await db_session.flush()

    with pytest.raises(IntegrityError):
        await db_session.execute(
            text("DELETE FROM posts WHERE id = :id"),
            {"id": post.id},
        )
        await db_session.flush()
