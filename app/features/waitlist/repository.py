from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.waitlist.models import WaitlistSignup


async def get_by_email(session: AsyncSession, *, email: str) -> WaitlistSignup | None:
    stmt = select(WaitlistSignup).where(WaitlistSignup.email == email)
    return (await session.execute(stmt)).scalar_one_or_none()


async def create(
    session: AsyncSession,
    *,
    email: str,
    business_name: str | None,
    city: str | None,
    phone: str | None,
    locale: str | None,
    source: str | None,
) -> WaitlistSignup:
    signup = WaitlistSignup(
        email=email,
        business_name=business_name,
        city=city,
        phone=phone,
        locale=locale,
        source=source,
    )
    session.add(signup)
    await session.flush()
    return signup
