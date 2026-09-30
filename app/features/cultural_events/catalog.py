"""Cultural-events catalog generator.

Ports pgblank-web/src/features/cultural-calendar/seed.ts event-for-event,
producing rows suitable for both the forward-only data migration and the
yearly `cultural_calendar.extend_catalog` job.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.features.cultural_events.umalqura import current_hijri_year, hijri_date_key


@dataclass(frozen=True)
class CatalogEvent:
    slug: str
    name: str
    name_ar: str
    start_date: date
    end_date: date
    kind: str  # religious | national | seasonal
    hijri_year: int | None
    source: str  # umm_al_qura | manual
    enabled_by_default: bool


def _parse(key: str) -> date:
    return date.fromisoformat(key)


def religious_events_for_hijri_year(hijri_year: int) -> list[CatalogEvent]:
    # Ramadan's last day and Eid Al-Fitr's first day both anchor to 1 Shawwal so
    # they always agree with whichever length Umm al-Qura assigns Ramadan.
    shawwal1 = hijri_date_key(hijri_year, 10, 1)
    ramadan_end = hijri_date_key(hijri_year, 10, 1, day_offset=-1)
    return [
        CatalogEvent(
            slug=f"islamic-new-year-{hijri_year}",
            name="Islamic New Year",
            name_ar="رأس السنة الهجرية",
            start_date=_parse(hijri_date_key(hijri_year, 1, 1)),
            end_date=_parse(hijri_date_key(hijri_year, 1, 1)),
            kind="religious",
            hijri_year=hijri_year,
            source="umm_al_qura",
            enabled_by_default=True,
        ),
        CatalogEvent(
            slug=f"ashura-{hijri_year}",
            name="Ashura",
            name_ar="عاشوراء",
            start_date=_parse(hijri_date_key(hijri_year, 1, 10)),
            end_date=_parse(hijri_date_key(hijri_year, 1, 10)),
            kind="religious",
            hijri_year=hijri_year,
            source="umm_al_qura",
            enabled_by_default=True,
        ),
        CatalogEvent(
            slug=f"mawlid-{hijri_year}",
            name="Mawlid",
            name_ar="المولد النبوي",
            start_date=_parse(hijri_date_key(hijri_year, 3, 12)),
            end_date=_parse(hijri_date_key(hijri_year, 3, 12)),
            kind="religious",
            hijri_year=hijri_year,
            source="umm_al_qura",
            enabled_by_default=True,
        ),
        CatalogEvent(
            slug=f"ramadan-{hijri_year}",
            name="Ramadan",
            name_ar="رمضان",
            start_date=_parse(hijri_date_key(hijri_year, 9, 1)),
            end_date=_parse(ramadan_end),
            kind="religious",
            hijri_year=hijri_year,
            source="umm_al_qura",
            enabled_by_default=True,
        ),
        CatalogEvent(
            slug=f"eid-al-fitr-{hijri_year}",
            name="Eid Al-Fitr",
            name_ar="عيد الفطر",
            start_date=_parse(shawwal1),
            end_date=_parse(hijri_date_key(hijri_year, 10, 3)),
            kind="religious",
            hijri_year=hijri_year,
            source="umm_al_qura",
            enabled_by_default=True,
        ),
        CatalogEvent(
            slug=f"arafah-day-{hijri_year}",
            name="Arafah Day",
            name_ar="يوم عرفة",
            start_date=_parse(hijri_date_key(hijri_year, 12, 9)),
            end_date=_parse(hijri_date_key(hijri_year, 12, 9)),
            kind="religious",
            hijri_year=hijri_year,
            source="umm_al_qura",
            enabled_by_default=True,
        ),
        CatalogEvent(
            slug=f"eid-al-adha-{hijri_year}",
            name="Eid Al-Adha",
            name_ar="عيد الأضحى",
            start_date=_parse(hijri_date_key(hijri_year, 12, 10)),
            end_date=_parse(hijri_date_key(hijri_year, 12, 13)),
            kind="religious",
            hijri_year=hijri_year,
            source="umm_al_qura",
            enabled_by_default=True,
        ),
    ]


def national_and_seasonal_events_for_gregorian_year(year: int) -> list[CatalogEvent]:
    return [
        CatalogEvent(
            slug=f"founding-day-{year}",
            name="Founding Day",
            name_ar="يوم التأسيس",
            start_date=date(year, 2, 22),
            end_date=date(year, 2, 22),
            kind="national",
            hijri_year=None,
            source="manual",
            enabled_by_default=True,
        ),
        CatalogEvent(
            slug=f"flag-day-{year}",
            name="Flag Day",
            name_ar="يوم العلم",
            start_date=date(year, 3, 11),
            end_date=date(year, 3, 11),
            kind="national",
            hijri_year=None,
            source="manual",
            enabled_by_default=True,
        ),
        CatalogEvent(
            slug=f"saudi-national-day-{year}",
            name="Saudi National Day",
            name_ar="اليوم الوطني السعودي",
            start_date=date(year, 9, 23),
            end_date=date(year, 9, 23),
            kind="national",
            hijri_year=None,
            source="manual",
            enabled_by_default=True,
        ),
        CatalogEvent(
            slug=f"riyadh-season-{year}",
            name="Riyadh Season",
            name_ar="موسم الرياض",
            start_date=date(year, 10, 15),
            end_date=date(year, 12, 31),
            kind="seasonal",
            hijri_year=None,
            source="manual",
            enabled_by_default=False,
        ),
        CatalogEvent(
            slug=f"back-to-school-{year}",
            name="Back to School",
            name_ar="العودة للمدارس",
            start_date=date(year, 8, 24),
            end_date=date(year, 8, 24),
            kind="seasonal",
            hijri_year=None,
            source="manual",
            enabled_by_default=True,
        ),
    ]


def build_events(
    *,
    hijri_years: list[int] | None = None,
    gregorian_years: list[int] | None = None,
    today: date | None = None,
) -> list[CatalogEvent]:
    """Two consecutive Hijri years (religious) and two consecutive Gregorian
    years (national/seasonal) around `today`, matching the frontend seed window.
    """
    today = today or date.today()
    if hijri_years is None:
        hy = current_hijri_year(today)
        hijri_years = [hy, hy + 1]
    if gregorian_years is None:
        gy = today.year
        gregorian_years = [gy, gy + 1]

    events: list[CatalogEvent] = []
    for hy in hijri_years:
        events.extend(religious_events_for_hijri_year(hy))
    for gy in gregorian_years:
        events.extend(national_and_seasonal_events_for_gregorian_year(gy))
    return events
