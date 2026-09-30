"""Credential pool selection — architecture/10 §2.

Picks the least-loaded active Zernio credential for a new brand profile or
for a connect that needs spare *account* slots (Zernio free keys = 2).
Concurrent selectors serialize by locking every active credential row
(stable alias order) before ranking.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy import case, func, nullsfirst, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.social_accounts.models import ZernioCredential, ZernioProfile
from app.integrations.errors import CredentialPoolExhausted


async def _lock_active_credentials(session: AsyncSession) -> None:
    """Serialize pool selection across workers (alias order avoids deadlocks)."""
    await session.scalars(
        select(ZernioCredential)
        .where(ZernioCredential.status == "active")
        .order_by(ZernioCredential.alias.asc())
        .with_for_update()
    )


def _live_profile_count_subq() -> sa.ScalarSelect[int]:
    return (
        select(func.count())
        .select_from(ZernioProfile)
        .where(
            ZernioProfile.credential_id == ZernioCredential.id,
            ZernioProfile.deleted_at.is_(None),
        )
        .correlate(ZernioCredential)
        .scalar_subquery()
    )


async def select_for_new_profile(
    session: AsyncSession,
    *,
    fill_to_tier: int = 3,
    exclude_ids: frozenset[uuid.UUID] | set[uuid.UUID] | None = None,
) -> ZernioCredential:
    """Select one credential for a new brand's Zernio profile.

    Policy (architecture/10 §2):
    - status = 'active'
    - live profile count (deleted_at IS NULL) < max_profiles
    - connected_accounts < max_accounts (spare Zernio account slots)
    - coalesce(rate_limited_until, '-infinity') < now()
    - fill_to_tier: prefer credentials with live count < tier before spreading
    """
    await _lock_active_credentials(session)

    live_count = _live_profile_count_subq()
    below_tier = case((live_count < fill_to_tier, 0), else_=1)
    rate_limited_until = func.coalesce(
        ZernioCredential.rate_limited_until,
        sa.literal_column("'-infinity'::timestamptz"),
    )

    where = [
        ZernioCredential.status == "active",
        live_count < ZernioCredential.max_profiles,
        ZernioCredential.connected_accounts < ZernioCredential.max_accounts,
        rate_limited_until < func.now(),
    ]
    if exclude_ids:
        where.append(ZernioCredential.id.notin_(tuple(exclude_ids)))

    stmt = (
        select(ZernioCredential)
        .where(*where)
        .order_by(
            below_tier.asc(),
            live_count.asc(),
            ZernioCredential.connected_accounts.asc(),
            nullsfirst(ZernioCredential.last_5xx_at.asc()),
            ZernioCredential.alias.asc(),
        )
        .limit(1)
    )

    credential = (await session.execute(stmt)).scalar_one_or_none()
    if credential is None:
        raise CredentialPoolExhausted(
            "no active Zernio credential with available profile/account capacity"
        )
    return credential


async def select_for_connect(
    session: AsyncSession,
    *,
    fill_to_tier: int = 3,
    exclude_ids: frozenset[uuid.UUID] | set[uuid.UUID] | None = None,
) -> ZernioCredential:
    """Select a credential that still has spare Zernio account slots for connect.

    Same ranking as ``select_for_new_profile`` — used when opening OAuth for an
    additional platform on a brand whose current key is at capacity.
    """
    return await select_for_new_profile(
        session, fill_to_tier=fill_to_tier, exclude_ids=exclude_ids
    )
