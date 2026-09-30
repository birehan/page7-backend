"""Rule-based industry defaults for POST .../brands/:brandId/generate.

Pure in-memory lookup — no DB, no network. Unrecognized industries fall through
to the mock's own three-pillar generic set rather than erroring.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class IndustryDefaults:
    pillars: list[dict[str, Any]]
    voice_adjectives: list[str]
    do_list: list[str]
    dont_list: list[str]
    banned_claims: list[str]
    dialect: str  # gulf | msa — overridden by the request body when provided
    colors: list[str]


_GENERIC = IndustryDefaults(
    pillars=[
        {"name": "What we offer", "description": "Core products/services", "weight": 3},
        {"name": "Behind the scenes", "description": "Team and process", "weight": 2},
        {"name": "Customer love", "description": "Reviews and testimonials", "weight": 2},
    ],
    voice_adjectives=["Friendly", "Trustworthy", "Local"],
    do_list=["Speak in warm Gulf Arabic first", "Keep English captions concise"],
    dont_list=["Avoid overly formal MSA for social captions"],
    banned_claims=["Unverified pricing or medical/financial claims"],
    dialect="gulf",
    colors=["#5B4CF5", "#111827", "#F9FAFB"],
)

_TABLE: dict[str, IndustryDefaults] = {
    "restaurant": IndustryDefaults(
        pillars=[
            {"name": "Menu highlights", "description": "Signature dishes", "weight": 3},
            {"name": "Behind the kitchen", "description": "Chefs and process", "weight": 2},
            {"name": "Customer reviews", "description": "Diners and moments", "weight": 2},
        ],
        voice_adjectives=["Warm", "Appetizing", "Welcoming"],
        do_list=["Lead with Gulf Arabic for social", "Name dishes in both languages"],
        dont_list=["Don't overpromise wait times"],
        banned_claims=[
            "Unverified pricing or medical/financial claims",
            "Guaranteed fresh/organic without certification",
        ],
        dialect="gulf",
        colors=["#5B4CF5", "#111827", "#F9FAFB"],
    ),
    "retail": IndustryDefaults(
        pillars=[
            {"name": "New arrivals", "description": "Fresh drops and collections", "weight": 3},
            {"name": "Styling tips", "description": "How to wear and pair", "weight": 2},
            {"name": "Customer looks", "description": "Real customers, real style", "weight": 2},
        ],
        voice_adjectives=["Stylish", "Helpful", "Current"],
        do_list=["Show product clearly", "Keep CTAs soft but clear"],
        dont_list=["Don't invent scarcity"],
        banned_claims=[
            "Unverified pricing or medical/financial claims",
            "Unverified discount percentages",
        ],
        dialect="gulf",
        colors=["#5B4CF5", "#111827", "#F9FAFB"],
    ),
    "real_estate": IndustryDefaults(
        pillars=[
            {"name": "Listings", "description": "Featured properties", "weight": 3},
            {"name": "Neighborhood guides", "description": "Areas and lifestyle", "weight": 2},
            {"name": "Client success stories", "description": "Closed deals", "weight": 2},
        ],
        voice_adjectives=["Professional", "Trustworthy", "Local"],
        do_list=[
            "Use clear Modern Standard Arabic for formal listings",
            "Keep English captions concise",
        ],
        dont_list=["Avoid unverified market claims"],
        banned_claims=[
            "Unverified pricing or medical/financial claims",
            "Guaranteed investment return",
        ],
        dialect="msa",
        colors=["#5B4CF5", "#111827", "#F9FAFB"],
    ),
    "healthcare_clinic": IndustryDefaults(
        pillars=[
            {"name": "Services offered", "description": "Treatments and specialties", "weight": 3},
            {"name": "Patient education", "description": "Clear, careful health info", "weight": 2},
            {"name": "Team spotlight", "description": "Doctors and care staff", "weight": 2},
        ],
        voice_adjectives=["Caring", "Clear", "Professional"],
        do_list=["Write in clear Modern Standard Arabic", "Keep English captions concise"],
        dont_list=["Never diagnose in a caption"],
        banned_claims=[
            "Unverified pricing or medical/financial claims",
            "Cure/treatment-outcome guarantees",
        ],
        dialect="msa",
        colors=["#5B4CF5", "#111827", "#F9FAFB"],
    ),
}


# Free-text / display-label aliases → known table keys (substring match on lowercased input).
_ALIAS_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        (
            "healthcare",
            "clinic",
            "dental",
            "hospital",
            "medical",
            "doctor",
            "عيادة",
            "مستشفى",
            "صحة",
            "أسنان",
        ),
        "healthcare_clinic",
    ),
    (("restaurant", "cafe", "café", "food", "مطعم", "مقهى"), "restaurant"),
    (("retail", "shop", "store", "boutique", "متجر", "تجزئة"), "retail"),
    (("real_estate", "realestate", "property", "عقارات", "عقار"), "real_estate"),
)


def lookup_industry_defaults(industry: str) -> IndustryDefaults:
    """Normalize with lowercase/trim; unrecognized strings get the generic fallback.

    Also matches common free-text labels (e.g. "Healthcare — Dental") via keyword aliases
    so onboarding quick-defaults stay useful when the org industry isn't an exact table key.
    """
    raw = industry.strip()
    key = raw.lower().replace(" ", "_").replace("-", "_")
    if key in _TABLE:
        return _TABLE[key]
    lowered = raw.lower()
    for keywords, mapped in _ALIAS_KEYWORDS:
        if any(token in lowered or token in key for token in keywords):
            return _TABLE[mapped]
    return _GENERIC


def guidelines_from_defaults(
    defaults: IndustryDefaults, *, dialect: str | None = None
) -> dict[str, Any]:
    chosen_dialect = dialect or defaults.dialect
    do_list = list(defaults.do_list)
    dont_list = list(defaults.dont_list)
    # Dialect-sensitive copy when falling back / overriding, matching the mock.
    if dialect == "msa" and defaults is _GENERIC:
        do_list = ["Write in clear Modern Standard Arabic", "Keep English captions concise"]
        dont_list = ["Avoid Gulf-colloquial slang for social captions"]
    elif dialect == "gulf" and defaults is _GENERIC:
        do_list = ["Speak in warm Gulf Arabic first", "Keep English captions concise"]
        dont_list = ["Avoid overly formal MSA for social captions"]
    return {
        "voiceAdjectives": list(defaults.voice_adjectives),
        "doList": do_list,
        "dontList": dont_list,
        "bannedClaims": list(defaults.banned_claims),
        "colors": list(defaults.colors),
        "dialect": chosen_dialect,
        "languages": ["ar", "en"],
    }


DEFAULT_CREATE_GUIDELINES: dict[str, Any] = {
    "voiceAdjectives": [],
    "doList": [],
    "dontList": [],
    "bannedClaims": [],
    "colors": ["#5B4CF5"],
    "dialect": "gulf",
    "languages": ["ar", "en"],
    "voice": {
        "tone": "friendly",
        "emojiUsage": "sparing",
        "ctaStyle": "direct",
        "rules": [],
    },
}
