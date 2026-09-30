"""Server-default smoke tests for Phase 11 image_generations tables."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.auth.models import User
from app.features.brands.models import Brand
from app.features.organizations.models import Organization
from app.features.visuals.models import ImageGeneration, ImageGenerationOutput


async def _seed(db_session: AsyncSession) -> tuple[Organization, Brand, User]:
    user = User(email="img-defaults@example.com", name="Img User")
    org = Organization(name="Img Org")
    db_session.add_all([user, org])
    await db_session.flush()
    brand = Brand(
        organization_id=org.id,
        name="Img Brand",
        industry="retail",
        city="Riyadh",
        website="https://example.sa",
        guidelines={},
    )
    db_session.add(brand)
    await db_session.flush()
    return org, brand, user


async def test_image_generations_defaults(db_session: AsyncSession) -> None:
    org, brand, user = await _seed(db_session)
    row = ImageGeneration(
        organization_id=org.id,
        brand_id=brand.id,
        requested_by=user.id,
        model="fal-ai/flux/dev",
        prompt="a coffee shop in Riyadh",
        style="photo",
        aspect="square",
        count=4,
    )
    db_session.add(row)
    await db_session.flush()
    assert row.status == "queued"
    assert row.provider == "fal"
    assert row.use_brand_colors is False
    assert row.decision_id is None
    assert row.id is not None
    assert row.created_at is not None


async def test_image_generation_outputs_unique_index(
    db_session: AsyncSession,
) -> None:
    org, brand, user = await _seed(db_session)
    gen = ImageGeneration(
        organization_id=org.id,
        brand_id=brand.id,
        requested_by=user.id,
        model="fal-ai/flux/dev",
        prompt="unique index check",
        style="flat",
        aspect="portrait",
        count=2,
    )
    db_session.add(gen)
    await db_session.flush()

    out = ImageGenerationOutput(
        organization_id=org.id,
        image_generation_id=gen.id,
        index=0,
        provider_url="https://fal.media/files/example.png",
        width=1024,
        height=1024,
    )
    db_session.add(out)
    await db_session.flush()

    dup = ImageGenerationOutput(
        organization_id=org.id,
        image_generation_id=gen.id,
        index=0,
        provider_url="https://fal.media/files/other.png",
        width=512,
        height=512,
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_image_generations_count_range(db_session: AsyncSession) -> None:
    org, brand, user = await _seed(db_session)
    row = ImageGeneration(
        organization_id=org.id,
        brand_id=brand.id,
        requested_by=user.id,
        model="fal-ai/flux/dev",
        prompt="too many",
        style="photo",
        aspect="square",
        count=5,
    )
    db_session.add(row)
    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_image_generations_style_check(db_session: AsyncSession) -> None:
    org, brand, user = await _seed(db_session)
    row = ImageGeneration(
        organization_id=org.id,
        brand_id=brand.id,
        requested_by=user.id,
        model="fal-ai/flux/dev",
        prompt="bad style",
        style="watercolor",
        aspect="square",
        count=1,
    )
    db_session.add(row)
    with pytest.raises(IntegrityError):
        await db_session.flush()
