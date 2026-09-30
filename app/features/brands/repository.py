from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utc_now
from app.features.brands.models import Brand, BrandCompetitor, BrandVersion, ContentPillar


async def list_brands(session: AsyncSession, *, organization_id: uuid.UUID) -> Sequence[Brand]:
    stmt = (
        select(Brand)
        .where(Brand.organization_id == organization_id, Brand.deleted_at.is_(None))
        .order_by(Brand.created_at.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def get_brand(
    session: AsyncSession, *, organization_id: uuid.UUID, brand_id: uuid.UUID
) -> Brand | None:
    stmt = select(Brand).where(
        Brand.id == brand_id,
        Brand.organization_id == organization_id,
        Brand.deleted_at.is_(None),
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_brand_including_deleted(
    session: AsyncSession, *, organization_id: uuid.UUID, brand_id: uuid.UUID
) -> Brand | None:
    """Used after a failed CAS to distinguish 404 from VERSION_CONFLICT."""
    stmt = select(Brand).where(Brand.id == brand_id, Brand.organization_id == organization_id)
    return (await session.execute(stmt)).scalar_one_or_none()


async def insert_brand(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    name: str,
    industry: str,
    city: str,
    website: str | None,
    guidelines: dict[str, Any],
) -> Brand:
    brand = Brand(
        organization_id=organization_id,
        name=name,
        industry=industry,
        city=city,
        website=website,
        guidelines=guidelines,
        version=1,
    )
    session.add(brand)
    await session.flush()
    return brand


async def cas_update_brand(
    session: AsyncSession,
    *,
    brand_id: uuid.UUID,
    organization_id: uuid.UUID,
    expected_version: int,
    name: str,
    industry: str,
    city: str,
    website: str | None,
    logo_url: str | None,
    guidelines: dict[str, Any],
) -> Brand | None:
    """Optimistic-concurrency UPDATE. Returns the updated row, or None if zero rows matched."""
    stmt = (
        update(Brand)
        .where(
            Brand.id == brand_id,
            Brand.organization_id == organization_id,
            Brand.version == expected_version,
            Brand.deleted_at.is_(None),
        )
        .values(
            name=name,
            industry=industry,
            city=city,
            website=website,
            logo_url=logo_url,
            guidelines=guidelines,
            version=Brand.version + 1,
            updated_at=utc_now(),
        )
        .returning(Brand)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def cas_update_logo_url(
    session: AsyncSession,
    *,
    brand_id: uuid.UUID,
    organization_id: uuid.UUID,
    expected_version: int,
    logo_url: str,
) -> Brand | None:
    """Bump version and set logo_url only (logo ingest path)."""
    stmt = (
        update(Brand)
        .where(
            Brand.id == brand_id,
            Brand.organization_id == organization_id,
            Brand.version == expected_version,
            Brand.deleted_at.is_(None),
        )
        .values(
            logo_url=logo_url,
            version=Brand.version + 1,
            updated_at=utc_now(),
        )
        .returning(Brand)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def soft_delete_brand(
    session: AsyncSession,
    *,
    brand: Brand,
    deleted_by: uuid.UUID,
    deleted_at: datetime | None = None,
) -> None:
    brand.deleted_at = deleted_at or utc_now()
    brand.deleted_by = deleted_by
    await session.flush()


async def list_pillars(
    session: AsyncSession, *, brand_id: uuid.UUID
) -> Sequence[ContentPillar]:
    stmt = (
        select(ContentPillar)
        .where(ContentPillar.brand_id == brand_id, ContentPillar.deleted_at.is_(None))
        .order_by(ContentPillar.position.asc())
    )
    return (await session.execute(stmt)).scalars().all()


async def insert_pillar(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    name: str,
    description: str = "",
    weight: Decimal | float = 1,
    position: int = 0,
    pillar_id: uuid.UUID | None = None,
) -> ContentPillar:
    kwargs: dict[str, Any] = {
        "organization_id": organization_id,
        "brand_id": brand_id,
        "name": name,
        "description": description,
        "weight": Decimal(str(weight)),
        "position": position,
    }
    if pillar_id is not None:
        kwargs["id"] = pillar_id
    pillar = ContentPillar(**kwargs)
    session.add(pillar)
    await session.flush()
    return pillar


async def soft_delete_pillars(
    session: AsyncSession,
    *,
    brand_id: uuid.UUID,
    deleted_by: uuid.UUID,
    pillar_ids: Sequence[uuid.UUID] | None = None,
) -> None:
    """Soft-delete pillars. If `pillar_ids` is None, cascade-delete all live ones."""
    stmt = select(ContentPillar).where(
        ContentPillar.brand_id == brand_id, ContentPillar.deleted_at.is_(None)
    )
    if pillar_ids is not None:
        stmt = stmt.where(ContentPillar.id.in_(list(pillar_ids)))
    pillars = (await session.execute(stmt)).scalars().all()
    now = utc_now()
    for pillar in pillars:
        pillar.deleted_at = now
        pillar.deleted_by = deleted_by
    await session.flush()


async def list_competitors(
    session: AsyncSession, *, brand_id: uuid.UUID
) -> Sequence[BrandCompetitor]:
    stmt = select(BrandCompetitor).where(BrandCompetitor.brand_id == brand_id)
    return (await session.execute(stmt)).scalars().all()


async def replace_competitors(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    competitors: Sequence[tuple[uuid.UUID | None, str, str]],
) -> list[BrandCompetitor]:
    """Replace the competitor set to match `competitors` (id|None, handle, platform)."""
    existing = list(await list_competitors(session, brand_id=brand_id))
    existing_by_id = {c.id: c for c in existing}
    keep_ids: set[uuid.UUID] = set()
    result: list[BrandCompetitor] = []

    for competitor_id, handle, platform in competitors:
        if competitor_id is not None and competitor_id in existing_by_id:
            row = existing_by_id[competitor_id]
            row.handle = handle
            row.platform = platform
            keep_ids.add(competitor_id)
            result.append(row)
        else:
            row = BrandCompetitor(
                id=competitor_id,
                organization_id=organization_id,
                brand_id=brand_id,
                handle=handle,
                platform=platform,
            )
            session.add(row)
            result.append(row)

    for row in existing:
        if row.id not in keep_ids:
            await session.delete(row)

    await session.flush()
    return result


async def delete_all_competitors(session: AsyncSession, *, brand_id: uuid.UUID) -> None:
    for row in await list_competitors(session, brand_id=brand_id):
        await session.delete(row)
    await session.flush()


async def insert_brand_version(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID,
    version: int,
    reason: str,
    author_user_id: uuid.UUID | None,
    snapshot: dict[str, Any],
) -> BrandVersion:
    row = BrandVersion(
        organization_id=organization_id,
        brand_id=brand_id,
        version=version,
        reason=reason,
        author_user_id=author_user_id,
        snapshot=snapshot,
    )
    session.add(row)
    await session.flush()
    return row


async def count_brand_versions(
    session: AsyncSession, *, brand_id: uuid.UUID
) -> int:
    from sqlalchemy import func

    stmt = select(func.count()).select_from(BrandVersion).where(BrandVersion.brand_id == brand_id)
    return int((await session.execute(stmt)).scalar_one())
