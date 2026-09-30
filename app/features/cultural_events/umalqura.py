"""Umm al-Qura Hijri ↔ Gregorian conversion.

Vendored from ICU4C's `islamcal.cpp` (`UMALQURA_MONTHLENGTH` and
`umAlQuraYrStartEstimateFix`, AH 1300–1600). Bit-identical to the browser's
`Intl.DateTimeFormat(..., { calendar: "islamic-umalqura" })` data source —
chosen over PyICU so the API image needs no system ICU dependency (see the
Phase 4 plan). Source: https://github.com/unicode-org/icu
(icu4c/source/i18n/islamcal.cpp).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

UMALQURA_YEAR_START = 1300
UMALQURA_YEAR_END = 1600

# Julian day number of the Islamic civil epoch (CE 622-07-19 Gregorian).
_CIVIL_EPOC = 1948440

# 12-bit month-length bitmaps: bit (11 - month0) set ⇒ that month has 30 days,
# otherwise 29. Index 0 = AH 1300.
_MONTHLENGTH: tuple[int, ...] = (
    0x0AAA, 0x0D54, 0x0EC9, 0x06D4, 0x06EA, 0x036C, 0x0AAD, 0x0555, 0x06A9, 0x0792,
    0x0BA9, 0x05D4, 0x0ADA, 0x055C, 0x0D2D, 0x0695, 0x074A, 0x0B54, 0x0B6A, 0x05AD,
    0x04AE, 0x0A4F, 0x0517, 0x068B, 0x06A5, 0x0AD5, 0x02D6, 0x095B, 0x049D, 0x0A4D,
    0x0D26, 0x0D95, 0x05AC, 0x09B6, 0x02BA, 0x0A5B, 0x052B, 0x0A95, 0x06CA, 0x0AE9,
    0x02F4, 0x0976, 0x02B6, 0x0956, 0x0ACA, 0x0BA4, 0x0BD2, 0x05D9, 0x02DC, 0x096D,
    0x054D, 0x0AA5, 0x0B52, 0x0BA5, 0x05B4, 0x09B6, 0x0557, 0x0297, 0x054B, 0x06A3,
    0x0752, 0x0B65, 0x056A, 0x0AAB, 0x052B, 0x0C95, 0x0D4A, 0x0DA5, 0x05CA, 0x0AD6,
    0x0957, 0x04AB, 0x094B, 0x0AA5, 0x0B52, 0x0B6A, 0x0575, 0x0276, 0x08B7, 0x045B,
    0x0555, 0x05A9, 0x05B4, 0x09DA, 0x04DD, 0x026E, 0x0936, 0x0AAA, 0x0D54, 0x0DB2,
    0x05D5, 0x02DA, 0x095B, 0x04AB, 0x0A55, 0x0B49, 0x0B64, 0x0B71, 0x05B4, 0x0AB5,
    0x0A55, 0x0D25, 0x0E92, 0x0EC9, 0x06D4, 0x0AE9, 0x096B, 0x04AB, 0x0A93, 0x0D49,
    0x0DA4, 0x0DB2, 0x0AB9, 0x04BA, 0x0A5B, 0x052B, 0x0A95, 0x0B2A, 0x0B55, 0x055C,
    0x04BD, 0x023D, 0x091D, 0x0A95, 0x0B4A, 0x0B5A, 0x056D, 0x02B6, 0x093B, 0x049B,
    0x0655, 0x06A9, 0x0754, 0x0B6A, 0x056C, 0x0AAD, 0x0555, 0x0B29, 0x0B92, 0x0BA9,
    0x05D4, 0x0ADA, 0x055A, 0x0AAB, 0x0595, 0x0749, 0x0764, 0x0BAA, 0x05B5, 0x02B6,
    0x0A56, 0x0E4D, 0x0B25, 0x0B52, 0x0B6A, 0x05AD, 0x02AE, 0x092F, 0x0497, 0x064B,
    0x06A5, 0x06AC, 0x0AD6, 0x055D, 0x049D, 0x0A4D, 0x0D16, 0x0D95, 0x05AA, 0x05B5,
    0x02DA, 0x095B, 0x04AD, 0x0595, 0x06CA, 0x06E4, 0x0AEA, 0x04F5, 0x02B6, 0x0956,
    0x0AAA, 0x0B54, 0x0BD2, 0x05D9, 0x02EA, 0x096D, 0x04AD, 0x0A95, 0x0B4A, 0x0BA5,
    0x05B2, 0x09B5, 0x04D6, 0x0A97, 0x0547, 0x0693, 0x0749, 0x0B55, 0x056A, 0x0A6B,
    0x052B, 0x0A8B, 0x0D46, 0x0DA3, 0x05CA, 0x0AD6, 0x04DB, 0x026B, 0x094B, 0x0AA5,
    0x0B52, 0x0B69, 0x0575, 0x0176, 0x08B7, 0x025B, 0x052B, 0x0565, 0x05B4, 0x09DA,
    0x04ED, 0x016D, 0x08B6, 0x0AA6, 0x0D52, 0x0DA9, 0x05D4, 0x0ADA, 0x095B, 0x04AB,
    0x0653, 0x0729, 0x0762, 0x0BA9, 0x05B2, 0x0AB5, 0x0555, 0x0B25, 0x0D92, 0x0EC9,
    0x06D2, 0x0AE9, 0x056B, 0x04AB, 0x0A55, 0x0D29, 0x0D54, 0x0DAA, 0x09B5, 0x04BA,
    0x0A3B, 0x049B, 0x0A4D, 0x0AAA, 0x0AD5, 0x02DA, 0x095D, 0x045E, 0x0A2E, 0x0C9A,
    0x0D55, 0x06B2, 0x06B9, 0x04BA, 0x0A5D, 0x052D, 0x0A95, 0x0B52, 0x0BA8, 0x0BB4,
    0x05B9, 0x02DA, 0x095A, 0x0B4A, 0x0DA4, 0x0ED1, 0x06E8, 0x0B6A, 0x056D, 0x0535,
    0x0695, 0x0D4A, 0x0DA8, 0x0DD4, 0x06DA, 0x055B, 0x029D, 0x062B, 0x0B15, 0x0B4A,
    0x0B95, 0x05AA, 0x0AAE, 0x092E, 0x0C8F, 0x0527, 0x0695, 0x06AA, 0x0AD6, 0x055D,
    0x029D,
)

# Correction applied to the linear year-start estimate. Index 0 = AH 1300.
_YR_START_FIX: tuple[int, ...] = (
     0,  0, -1,  0, -1,  0,  0,  0,  0,  0, -1,  0,  0,  0,  0,  0,  0,  0, -1,  0,
     1,  0,  1,  1,  0,  0,  0,  0,  1,  0,  0,  0,  0,  0,  0,  0,  1,  0,  0,  0,
     0,  0,  1,  0,  0, -1, -1,  0,  0,  0,  1,  0,  0, -1,  0,  0,  0,  1,  1,  0,
     0,  0,  0,  0,  0,  0,  0, -1,  0,  0,  0,  1,  1,  0,  0, -1,  0,  1,  0,  1,
     1,  0,  0, -1,  0,  1,  0,  0,  0, -1,  0,  1,  0,  1,  0,  0,  0, -1,  0,  0,
     0,  0, -1, -1,  0, -1,  0,  1,  0,  0,  0, -1,  0,  0,  0,  1,  0,  0,  0,  0,
     0,  1,  0,  0, -1, -1,  0,  0,  0,  1,  0,  0, -1, -1,  0, -1,  0,  0, -1, -1,
     0, -1,  0, -1,  0,  0, -1, -1,  0,  0,  0,  0,  0,  0, -1,  0,  1,  0,  1,  1,
     0,  0, -1,  0,  1,  0,  0,  0,  0,  0,  1,  0,  1,  0,  0,  0, -1,  0,  1,  0,
     0, -1, -1,  0,  0,  0,  1,  0,  0,  0,  0,  0,  0,  0,  1,  0,  0,  0,  0,  0,
     1,  0,  0, -1,  0,  0,  0,  1,  1,  0,  0, -1,  0,  1,  0,  1,  1,  0,  0,  0,
     0,  1,  0,  0,  0, -1,  0,  0,  0,  1,  0,  0,  0, -1,  0,  0,  0,  0,  0, -1,
     0, -1,  0,  1,  0,  0,  0, -1,  0,  1,  0,  1,  0,  0,  0,  0,  0,  1,  0,  0,
    -1,  0,  0,  0,  0,  1,  0,  0,  0, -1,  0,  0,  0,  0, -1, -1,  0, -1,  0,  1,
     0,  0, -1, -1,  0,  0,  1,  1,  0,  0, -1,  0,  0,  0,  0,  1,  0,  0,  0,  0,
     1,
)


class UmalquraOutOfRangeError(ValueError):
    """Raised when a date falls outside the AH 1300–1600 table."""


def _month_length(year: int, month0: int) -> int:
    mask = 1 << (11 - month0)
    return 29 + (1 if _MONTHLENGTH[year - UMALQURA_YEAR_START] & mask else 0)


def _year_start(year: int) -> int:
    """Days from the Hijri epoch (origin 0) on which `year` starts."""
    y = year - UMALQURA_YEAR_START
    estimate = int((354.36720 * y) + 460322.05 + 0.5)
    return estimate + _YR_START_FIX[y]


def _month_start(year: int, month0: int) -> int:
    ms = _year_start(year)
    for i in range(month0):
        ms += _month_length(year, i)
    return ms


def _gregorian_to_jdn(year: int, month: int, day: int) -> int:
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    return day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045


def _jdn_to_gregorian(jdn: int) -> tuple[int, int, int]:
    a = jdn + 32044
    b = (4 * a + 3) // 146097
    c = a - (146097 * b) // 4
    d = (4 * c + 3) // 1461
    e = c - (1461 * d) // 4
    m = (5 * e + 2) // 153
    day = e - (153 * m + 2) // 5 + 1
    month = m + 3 - 12 * (m // 10)
    year = 100 * b + d - 4800 + m // 10
    return year, month, day


def _assert_in_range(year: int) -> None:
    if year < UMALQURA_YEAR_START or year > UMALQURA_YEAR_END:
        raise UmalquraOutOfRangeError(
            f"Hijri year {year} is outside the Umm al-Qura table "
            f"({UMALQURA_YEAR_START}–{UMALQURA_YEAR_END})"
        )


def hijri_to_gregorian(hijri_year: int, hijri_month: int, hijri_day: int) -> date:
    """Convert a Hijri (Umm al-Qura) y/m/d to a Gregorian `date` (UTC calendar day)."""
    _assert_in_range(hijri_year)
    if not 1 <= hijri_month <= 12:
        raise ValueError(f"hijri_month must be 1–12, got {hijri_month}")
    ml = _month_length(hijri_year, hijri_month - 1)
    if not 1 <= hijri_day <= ml:
        raise ValueError(
            f"hijri_day {hijri_day} is out of range for {hijri_year}-{hijri_month} (1–{ml})"
        )
    days = _month_start(hijri_year, hijri_month - 1) + (hijri_day - 1)
    y, m, d = _jdn_to_gregorian(days + _CIVIL_EPOC)
    return date(y, m, d)


def gregorian_to_hijri(gregorian: date) -> tuple[int, int, int]:
    """Convert a Gregorian `date` to (hijri_year, hijri_month, hijri_day)."""
    days = _gregorian_to_jdn(gregorian.year, gregorian.month, gregorian.day) - _CIVIL_EPOC
    if days < _year_start(UMALQURA_YEAR_START):
        raise UmalquraOutOfRangeError(
            f"{gregorian.isoformat()} is before the Umm al-Qura table start"
        )
    year = int((days - (460322.05 + 0.5)) / 354.36720) + UMALQURA_YEAR_START - 1
    month = 0
    d = 1
    while d > 0:
        year += 1
        if year > UMALQURA_YEAR_END:
            raise UmalquraOutOfRangeError(
                f"{gregorian.isoformat()} is after the Umm al-Qura table end"
            )
        d = days - _year_start(year) + 1
        length = sum(_month_length(year, i) for i in range(12))
        if d == length:
            month = 11
            break
        if d < length:
            month = 0
            month_len = _month_length(year, month)
            while d > month_len:
                d -= month_len
                month += 1
                month_len = _month_length(year, month)
            break
    day_of_month = days - _month_start(year, month) + 1
    return year, month + 1, day_of_month


def current_hijri_year(when: date | datetime | None = None) -> int:
    """Hijri year of `when` (default: today in UTC), matching the frontend's
    `currentHijriYear` which reads ICU via `Intl` against UTC.
    """
    if when is None:
        g = datetime.now(UTC).date()
    elif isinstance(when, datetime):
        g = when.astimezone(UTC).date() if when.tzinfo else when.date()
    else:
        g = when
    year, _month, _day = gregorian_to_hijri(g)
    return year


def hijri_date_key(
    hijri_year: int, hijri_month: int, hijri_day: int, *, day_offset: int = 0
) -> str:
    """Gregorian `yyyy-MM-dd` for a Hijri date, optionally shifted by whole days —
    ports `hijriDateKey` from pgblank-web/src/shared/lib/hijri.ts.
    """
    base = hijri_to_gregorian(hijri_year, hijri_month, hijri_day)
    if day_offset:
        base = base + timedelta(days=day_offset)
    return base.isoformat()
