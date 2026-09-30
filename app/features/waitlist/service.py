from __future__ import annotations

from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.time import utc_now
from app.features.waitlist import repository
from app.features.waitlist.models import WaitlistSignup
from app.infrastructure.ratelimit.limiter import RateLimiter

_IP_LIMIT = 10
_IP_WINDOW = timedelta(minutes=15)


async def _rate_limit(*, ip: str | None) -> None:
    if ip is None:
        return
    limiter = RateLimiter()
    count = await limiter.increment(bucket=f"waitlist:ip:{ip}", now=utc_now(), window=_IP_WINDOW)
    if count > _IP_LIMIT:
        raise ApiError("RATE_LIMIT", "Too many attempts. Try again later.", status_code=429)


async def join(
    session: AsyncSession,
    *,
    email: str,
    business_name: str | None,
    city: str | None,
    phone: str | None,
    locale: str | None,
    source: str | None,
    ip: str | None,
) -> tuple[WaitlistSignup, bool]:
    """Idempotent join: a repeat submission with the same email is a normal
    outcome (someone re-visiting the page), not an error — it reports
    `already_joined=True` rather than a 409, so the form never has to show a
    scary error for the happy case of "yes, we already have you".
    """
    await _rate_limit(ip=ip)

    normalized_email = email.strip().lower()
    existing = await repository.get_by_email(session, email=normalized_email)
    if existing is not None:
        return existing, True

    created = await repository.create(
        session,
        email=normalized_email,
        business_name=business_name.strip() if business_name else None,
        city=city.strip() if city else None,
        phone=phone.strip() if phone else None,
        locale=locale,
        source=source,
    )
    return created, False
