from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.cultural_events.catalog import CatalogEvent
from app.features.cultural_events.models import CulturalEvent, OrganizationCulturalEventSetting


async def list_events_for_org(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> Sequence[tuple[CulturalEvent, bool]]:
    """Catalog LEFT JOIN org overlay; enabled = coalesce(settings, default)."""
    enabled_expr = func.coalesce(
        OrganizationCulturalEventSetting.enabled,
        CulturalEvent.enabled_by_default,
    )
    stmt = (
        select(CulturalEvent, enabled_expr)
        .outerjoin(
            OrganizationCulturalEventSetting,
            (OrganizationCulturalEventSetting.cultural_event_id == CulturalEvent.id)
            & (OrganizationCulturalEventSetting.organization_id == organization_id),
        )
        .order_by(CulturalEvent.start_date.asc())
    )
    rows = (await session.execute(stmt)).all()
    return [(event, bool(enabled)) for event, enabled in rows]


async def get_event(session: AsyncSession, *, event_id: uuid.UUID) -> CulturalEvent | None:
    return await session.get(CulturalEvent, event_id)


async def get_org_setting(
    session: AsyncSession, *, organization_id: uuid.UUID, event_id: uuid.UUID
) -> OrganizationCulturalEventSetting | None:
    return await session.get(
        OrganizationCulturalEventSetting, (organization_id, event_id)
    )


async def upsert_org_setting(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    event_id: uuid.UUID,
    enabled: bool,
) -> OrganizationCulturalEventSetting:
    existing = await get_org_setting(
        session, organization_id=organization_id, event_id=event_id
    )
    if existing is not None:
        existing.enabled = enabled
        await session.flush()
        return existing
    row = OrganizationCulturalEventSetting(
        organization_id=organization_id,
        cultural_event_id=event_id,
        enabled=enabled,
    )
    session.add(row)
    await session.flush()
    return row


async def insert_catalog_events(
    session: AsyncSession, *, events: Sequence[CatalogEvent]
) -> int:
    """Idempotent insert — ON CONFLICT (slug) DO NOTHING. Returns rows inserted."""
    if not events:
        return 0
    values = [
        {
            "slug": e.slug,
            "name": e.name,
            "name_ar": e.name_ar,
            "start_date": e.start_date,
            "end_date": e.end_date,
            "kind": e.kind,
            "hijri_year": e.hijri_year,
            "source": e.source,
            "enabled_by_default": e.enabled_by_default,
        }
        for e in events
    ]
    stmt = (
        insert(CulturalEvent)
        .values(values)
        .on_conflict_do_nothing(index_elements=["slug"])
        .returning(CulturalEvent.id)
    )
    result = await session.execute(stmt)
    return len(result.all())


async def max_forward_coverage_years(
    session: AsyncSession, *, today: date | None = None
) -> float:
    """Years of future coverage remaining in the catalog (for the gauge / extend job)."""
    today = today or date.today()
    stmt = select(func.max(CulturalEvent.start_date))
    latest = (await session.execute(stmt)).scalar_one_or_none()
    if latest is None:
        return 0.0
    return (latest - today).days / 365.25
