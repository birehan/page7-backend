from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.features import audit
from app.features.cultural_events import repository
from app.features.cultural_events.catalog import build_events
from app.features.cultural_events.models import CulturalEvent
from app.features.cultural_events.schemas import CulturalEventOut


def _to_out(event: CulturalEvent, *, enabled: bool) -> CulturalEventOut:
    end: date | None = event.end_date
    if end == event.start_date:
        end = None
    return CulturalEventOut(
        id=event.id,
        name=event.name,
        name_ar=event.name_ar,
        date=event.start_date,
        end_date=end,
        kind=event.kind,
        enabled=enabled,
    )


async def list_cultural_events(
    session: AsyncSession, *, organization_id: uuid.UUID
) -> list[CulturalEventOut]:
    rows = await repository.list_events_for_org(session, organization_id=organization_id)
    return [_to_out(event, enabled=enabled) for event, enabled in rows]


async def get_cultural_event(
    session: AsyncSession, *, event_id: uuid.UUID
) -> CulturalEventOut | None:
    event = await repository.get_event(session, event_id=event_id)
    if event is None:
        return None
    return _to_out(event, enabled=True)


async def toggle_cultural_event(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    event_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    actor_name: str,
) -> CulturalEventOut:
    event = await repository.get_event(session, event_id=event_id)
    if event is None:
        raise ApiError("NOT_FOUND", "Cultural event not found", status_code=404)

    setting = await repository.get_org_setting(
        session, organization_id=organization_id, event_id=event_id
    )
    current = setting.enabled if setting is not None else event.enabled_by_default
    new_enabled = not current
    await repository.upsert_org_setting(
        session,
        organization_id=organization_id,
        event_id=event_id,
        enabled=new_enabled,
    )
    await audit.record(
        session,
        organization_id=organization_id,
        actor_kind="user",
        actor_ref=str(actor_user_id),
        actor_name=actor_name,
        actor_user_id=actor_user_id,
        action="cultural_event.toggled",
        target_type="cultural_event",
        target_id=event_id,
        meta={"enabled": new_enabled},
    )
    return _to_out(event, enabled=new_enabled)


async def extend_catalog(session: AsyncSession, *, today: date | None = None) -> int:
    """Insert the next year's events when forward coverage drops below two years.

    Idempotent via ON CONFLICT (slug) DO NOTHING.
    """
    today = today or date.today()
    coverage = await repository.max_forward_coverage_years(session, today=today)
    if coverage >= 2.0:
        return 0
    from app.features.cultural_events.umalqura import current_hijri_year

    hy = current_hijri_year(today)
    gy = today.year
    # Seed current+next and also current+2 so a lagging catalog catches up.
    events = build_events(
        hijri_years=[hy, hy + 1, hy + 2],
        gregorian_years=[gy, gy + 1, gy + 2],
        today=today,
    )
    return await repository.insert_catalog_events(session, events=events)
