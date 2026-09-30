"""Server-default smoke tests for Phase 5 media tables."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.features.auth.models import User
from app.features.brands.models import Brand
from app.features.media.models import MediaAsset, MediaVariant, UploadIntent
from app.features.organizations.models import Organization


async def test_media_tables_defaults(db_session: AsyncSession) -> None:
    user = User(email="media-defaults@example.com", name="Media User")
    org = Organization(name="Media Org")
    db_session.add_all([user, org])
    await db_session.flush()

    brand = Brand(
        organization_id=org.id,
        name="Media Brand",
        industry="retail",
        city="Riyadh",
        guidelines={},
    )
    db_session.add(brand)
    await db_session.flush()

    intent = UploadIntent(
        organization_id=org.id,
        brand_id=brand.id,
        created_by=user.id,
        r2_bucket="pgblank",
        r2_key="tmp/defaults/original",
        original_filename="photo.jpg",
        content_type="image/jpeg",
        declared_size=1024,
        expires_at=datetime.now(UTC) + timedelta(hours=24),
    )
    db_session.add(intent)
    await db_session.flush()
    assert intent.completed_at is None
    assert intent.created_at is not None

    asset = MediaAsset(
        organization_id=org.id,
        brand_id=brand.id,
        kind="image",
        source="upload",
        r2_bucket="pgblank",
        r2_key="orgs/o/brands/b/media/x/original",
        content_type="image/jpeg",
        size_bytes=1024,
        width=800,
        height=600,
    )
    db_session.add(asset)
    await db_session.flush()
    assert asset.status == "ready"
    assert asset.alt_ar == ""
    assert asset.alt_en == ""
    assert asset.tags == []

    variant = MediaVariant(
        organization_id=org.id,
        media_asset_id=asset.id,
        purpose="thumb",
        aspect="original",
        r2_key="orgs/o/brands/b/media/x/variants/thumb_original_320.webp",
        content_type="image/webp",
        width=320,
        height=240,
        size_bytes=100,
        source_hash="abc",
    )
    db_session.add(variant)
    await db_session.flush()
    assert variant.created_at is not None
