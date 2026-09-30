"""MEDIA_IN_USE guard and brand-delete cascade for posts (Phase 6 Stage 6)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.ids import new_uuid7
from app.features.auth.models import User
from app.features.brands.models import Brand
from app.features.media.models import MediaAsset
from app.features.organizations.models import Organization
from app.features.posts.models import Post, PostMedia
from app.features.posts.repository import (
    media_referenced_by_active_posts,
    soft_delete_brand_posts,
)
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def test_media_referenced_by_scheduled_post(db_session: AsyncSession) -> None:
    user = User(email="media-in-use@example.com", name="Media Guard")
    org = Organization(name="Media Guard Org")
    db_session.add_all([user, org])
    await db_session.flush()
    brand = Brand(
        organization_id=org.id,
        name="Guard Brand",
        industry="retail",
        city="Riyadh",
        guidelines={},
    )
    db_session.add(brand)
    await db_session.flush()

    asset = MediaAsset(
        organization_id=org.id,
        brand_id=brand.id,
        kind="image",
        source="upload",
        r2_bucket="pgblank",
        r2_key=f"orgs/{org.id}/brands/{brand.id}/media/{new_uuid7()}/original",
        content_type="image/jpeg",
        size_bytes=1024,
        width=800,
        height=600,
    )
    db_session.add(asset)
    await db_session.flush()

    assert await media_referenced_by_active_posts(
        db_session, media_asset_id=asset.id
    ) is False

    post = Post(
        organization_id=org.id,
        brand_id=brand.id,
        platform="instagram",
        status="scheduled",
        scheduled_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        created_by=user.id,
        variants=[],
        risk={"score": 0, "reasons": []},
        risk_score=Decimal("0"),
    )
    db_session.add(post)
    await db_session.flush()
    db_session.add(
        PostMedia(
            organization_id=org.id,
            brand_id=brand.id,
            post_id=post.id,
            media_asset_id=asset.id,
            position=0,
        )
    )
    await db_session.flush()

    assert await media_referenced_by_active_posts(
        db_session, media_asset_id=asset.id
    ) is True


async def test_soft_delete_brand_posts(db_session: AsyncSession) -> None:
    user = User(email="cascade-posts@example.com", name="Cascade")
    org = Organization(name="Cascade Org")
    db_session.add_all([user, org])
    await db_session.flush()
    brand = Brand(
        organization_id=org.id,
        name="Cascade Brand",
        industry="retail",
        city="Riyadh",
        guidelines={},
    )
    db_session.add(brand)
    await db_session.flush()
    post = Post(
        organization_id=org.id,
        brand_id=brand.id,
        platform="instagram",
        scheduled_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        created_by=user.id,
        variants=[],
        risk={"score": 0, "reasons": []},
        risk_score=Decimal("0"),
    )
    db_session.add(post)
    await db_session.flush()

    count = await soft_delete_brand_posts(
        db_session, brand_id=brand.id, deleted_by=user.id
    )
    assert count == 1
    await db_session.refresh(post)
    assert post.deleted_at is not None


async def test_delete_media_returns_media_in_use_when_scheduled(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    """HTTP-level guard: deleting an asset on a scheduled post is 409 MEDIA_IN_USE."""
    org_id, user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    brand_resp = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={"name": f"InUse {new_uuid7().hex[:6]}", "industry": "retail", "city": "Riyadh"},
    )
    assert brand_resp.status_code == 201
    brand_id = brand_resp.json()["id"]

    asset = MediaAsset(
        organization_id=org_id,
        brand_id=brand_id,
        kind="image",
        source="upload",
        r2_bucket="pgblank",
        r2_key=f"orgs/{org_id}/brands/{brand_id}/media/{new_uuid7()}/original",
        content_type="image/jpeg",
        size_bytes=1024,
        width=800,
        height=600,
        uploaded_by=user_id,
    )
    db_session.add(asset)
    await db_session.flush()

    post = Post(
        organization_id=org_id,
        brand_id=brand_id,
        platform="instagram",
        status="scheduled",
        scheduled_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        created_by=user_id,
        variants=[],
        risk={"score": 0, "reasons": []},
        risk_score=Decimal("0"),
    )
    db_session.add(post)
    await db_session.flush()
    db_session.add(
        PostMedia(
            organization_id=org_id,
            brand_id=brand_id,
            post_id=post.id,
            media_asset_id=asset.id,
            position=0,
        )
    )
    await db_session.commit()

    deleted = await client.delete(
        f"/v1/orgs/{org_id}/brands/{brand_id}/media/{asset.id}"
    )
    assert deleted.status_code == 409, deleted.text
    assert deleted.json()["error"]["code"] == "MEDIA_IN_USE"
