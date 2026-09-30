"""Line-for-line port of pgblank-web risk.ts + prayer-times.ts.

Parity is proven by backend/tests/fixtures/risk_parity.json, generated from
the frontend's computeRisk via scripts/gen-risk-fixtures.ts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from app.core.time import riyadh_date_key, riyadh_datetime_to_utc

CTA_HINTS = [
    "احجز",
    "تواصل",
    "زور",
    "book",
    "visit",
    "dm",
    "call",
    "order",
    "شوفوا",
    "تعالوا",
]

AD_DISCLOSURE_MARKERS = ["#إعلان", "#اعلان", "#ad", "#sponsored", "#مُمول", "#ممول"]

SENSITIVE_KEYWORDS: dict[str, list[str]] = {
    "religion_politics": [
        "دين",
        "مذهب",
        "سياس",
        "حكومة",
        "انتخاب",
        "religion",
        "politic",
        "election",
        "government",
    ],
    "gender_mixing": ["اختلاط", "mixed-gender", "mixed gender"],
    "alcohol_gambling": [
        "كحول",
        "خمر",
        "قمار",
        "يانصيب",
        "alcohol",
        "beer",
        "wine",
        "gambling",
        "casino",
        "lottery",
        "bet",
    ],
    "tobacco_vaping": [
        "تبغ",
        "سجائر",
        "فيب",
        "شيشة",
        "tobacco",
        "cigarette",
        "vape",
        "vaping",
        "shisha",
    ],
    "modesty": ["عاري", "عري", "nude", "nudity"],
}

MEDICAL_FINANCIAL_KEYWORDS = [
    "يشفي",
    "علاج مضمون",
    "ضمان الشفاء",
    "عائد مضمون",
    "ربح مضمون",
    "استثمار مضمون",
    "cures",
    "guaranteed cure",
    "guaranteed return",
    "guaranteed profit",
    "risk-free investment",
]

PLATFORM_CAPTION_LIMIT: dict[str, int] = {
    "instagram": 2200,
    "facebook": 63206,
    "tiktok": 2200,
    "snapchat": 250,
    "whatsapp": 1024,
}

SEVERITY_WEIGHT: dict[str, float] = {"high": 0.4, "medium": 0.2, "low": 0.08}

PrayerKey = Literal["fajr", "dhuhr", "asr", "maghrib", "isha"]

WINTER: list[tuple[PrayerKey, str]] = [
    ("fajr", "05:10"),
    ("dhuhr", "12:05"),
    ("asr", "15:20"),
    ("maghrib", "17:35"),
    ("isha", "19:05"),
]
SUMMER: list[tuple[PrayerKey, str]] = [
    ("fajr", "04:05"),
    ("dhuhr", "12:15"),
    ("asr", "15:45"),
    ("maghrib", "18:45"),
    ("isha", "20:15"),
]

CITY_OFFSET_MINUTES: dict[str, int] = {
    "Riyadh": 0,
    "Jeddah": 15,
    "Makkah": 16,
    "Madinah": 13,
    "Dammam": -14,
}


@dataclass(frozen=True)
class RiskReason:
    code: str
    severity: Literal["low", "medium", "high"]
    params: dict[str, str | int] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "severity": self.severity}
        if self.params is not None:
            out["params"] = self.params
        return out


@dataclass(frozen=True)
class RiskReport:
    score: float
    reasons: list[RiskReason] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"score": self.score, "reasons": [r.to_dict() for r in self.reasons]}


def caption_limit_for(platform: str) -> int:
    return PLATFORM_CAPTION_LIMIT.get(platform, 2200)


def _contains_any(haystack: str, needles: list[str]) -> str | None:
    lower = haystack.lower()
    for needle in needles:
        if needle.lower() in lower:
            return needle
    return None


def _to_minutes(hhmm: str) -> int:
    h, m = (int(part) for part in hhmm.split(":"))
    return h * 60 + m


def _shift_time(hhmm: str, minutes: int) -> str:
    h, m = (int(part) for part in hhmm.split(":"))
    total = ((h * 60 + m + minutes) % 1440 + 1440) % 1440
    return f"{total // 60:02d}:{total % 60:02d}"


def prayer_times_for(date_key: str, city: str | None = None) -> list[tuple[PrayerKey, str]]:
    month = int(date_key[5:7])
    base = SUMMER if 4 <= month <= 9 else WINTER
    offset = CITY_OFFSET_MINUTES.get(city, 0) if city else 0
    if offset == 0:
        return list(base)
    return [(key, _shift_time(time, offset)) for key, time in base]


def find_overlapping_prayer(
    date_key: str,
    hhmm: str,
    city: str | None = None,
    window_minutes: int = 20,
) -> PrayerKey | None:
    target = _to_minutes(hhmm)
    for key, time in prayer_times_for(date_key, city):
        if abs(_to_minutes(time) - target) <= window_minutes:
            return key
    return None


def riyadh_wall_time(instant: datetime) -> str:
    """HH:mm in Asia/Riyadh — mirrors format.ts riyadhWallTime."""
    from zoneinfo import ZoneInfo

    local = instant.astimezone(ZoneInfo("Asia/Riyadh"))
    return f"{local.hour:02d}:{local.minute:02d}"


def compute_risk(
    *,
    variants: list[dict[str, Any]],
    platforms: list[str],
    banned_claims: list[str] | None = None,
    media_alts: list[str] | None = None,
    scheduled_at: datetime | None = None,
    city: str | None = None,
    is_paid: bool = False,
) -> RiskReport:
    """Pure, deterministic risk scoring — port of risk.ts computeRisk."""
    reasons: list[RiskReason] = []
    banned = banned_claims or []
    ar = next((v for v in variants if v.get("lang") == "ar"), None)

    for variant in variants:
        caption = str(variant.get("caption", ""))
        hashtags = list(variant.get("hashtags") or [])

        for claim in banned:
            if claim.lower() in caption.lower():
                reasons.append(
                    RiskReason("banned_claim", "high", {"match": claim})
                )

        medical_match = _contains_any(caption, MEDICAL_FINANCIAL_KEYWORDS)
        if medical_match:
            reasons.append(
                RiskReason("banned_claim", "high", {"match": medical_match})
            )

        for keywords in SENSITIVE_KEYWORDS.values():
            match = _contains_any(caption, keywords)
            if match:
                reasons.append(
                    RiskReason("cultural_sensitivity", "medium", {"match": match})
                )
                break

        for platform in platforms:
            limit = caption_limit_for(platform)
            if len(caption) > limit:
                reasons.append(
                    RiskReason(
                        "caption_over_limit",
                        "medium",
                        {"platform": platform, "limit": limit},
                    )
                )

        if len(hashtags) > 30:
            reasons.append(RiskReason("too_many_hashtags", "low", {"max": 30}))

    if ar is not None:
        ar_caption = str(ar.get("caption", "")).lower()
        if not any(hint in ar_caption for hint in CTA_HINTS):
            reasons.append(RiskReason("missing_cta", "low"))

    if is_paid:
        disclosed = any(
            any(
                marker.lower() in str(v.get("caption", "")).lower()
                for marker in AD_DISCLOSURE_MARKERS
            )
            for v in variants
        )
        if not disclosed:
            reasons.append(RiskReason("missing_ad_disclosure", "high"))

    if media_alts is not None and any(not (alt or "").strip() for alt in media_alts):
        reasons.append(RiskReason("no_alt_text", "low"))

    if scheduled_at is not None:
        date_key = riyadh_date_key(scheduled_at)
        wall = riyadh_wall_time(scheduled_at)
        overlap = find_overlapping_prayer(date_key, wall, city)
        if overlap is not None:
            reasons.append(
                RiskReason("prayer_time_overlap", "low", {"prayer": overlap})
            )

    raw = sum(SEVERITY_WEIGHT[r.severity] for r in reasons)
    score = round(min(1.0, raw), 2)
    return RiskReport(score=score, reasons=reasons)


__all__ = [
    "RiskReason",
    "RiskReport",
    "caption_limit_for",
    "compute_risk",
    "find_overlapping_prayer",
    "prayer_times_for",
    "riyadh_datetime_to_utc",
    "riyadh_wall_time",
]
