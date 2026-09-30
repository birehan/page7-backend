"""Credential pool FOR UPDATE concurrency — pure least-loaded spread."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.engine import get_engine
from app.features.brands.models import Brand
from app.features.organizations.models import Organization
from app.features.social_accounts.models import ZernioCredential, ZernioProfile
from app.integrations.errors import CredentialPoolExhausted
from app.integrations.social import select_for_connect, select_for_new_profile


@pytest.mark.asyncio
async def test_concurrent_select_spreads_with_fill_to_tier_one() -> None:
    """With fill_to_tier=1, two serialized FOR UPDATE selects must pick different
    credentials once the first inserts a live profile (architecture/10 §2).
    """
    factory = async_sessionmaker(get_engine(), expire_on_commit=False)

    async def _pick_and_pin(
        org_id: uuid.UUID, brand_id: uuid.UUID
    ) -> uuid.UUID:
        async with factory() as session:
            cred = await select_for_new_profile(session, fill_to_tier=1)
            session.add(
                ZernioProfile(
                    organization_id=org_id,
                    brand_id=brand_id,
                    credential_id=cred.id,
                    zernio_profile_id=f"prof_{uuid.uuid4().hex[:8]}",
                    status="active",
                )
            )
            await session.commit()
            return cred.id

    async with factory() as setup:
        # Extra aliases (e.g. t4) with zero live profiles collapse fill_to_tier=1
        # onto a single empty key — park them so the spread assertion stays valid.
        extras = (
            await setup.execute(
                select(ZernioCredential).where(
                    ZernioCredential.alias.notin_(("t1", "t2", "t3"))
                )
            )
        ).scalars().all()
        for extra in extras:
            extra.status = "disabled"
        # Prior suite runs leave connected_accounts / live profiles elevated;
        # without a reset only one key stays eligible and the spread is impossible.
        core = (
            await setup.execute(
                select(ZernioCredential).where(
                    ZernioCredential.alias.in_(("t1", "t2", "t3"))
                )
            )
        ).scalars().all()
        core_ids = [c.id for c in core]
        for cred in core:
            cred.status = "active"
            cred.connected_accounts = 0
        if core_ids:
            await setup.execute(
                update(ZernioProfile)
                .where(
                    ZernioProfile.credential_id.in_(core_ids),
                    ZernioProfile.deleted_at.is_(None),
                )
                .values(deleted_at=func.now())
            )
        org_a = Organization(name=f"Pool Org A {uuid.uuid4().hex[:6]}")
        org_b = Organization(name=f"Pool Org B {uuid.uuid4().hex[:6]}")
        setup.add_all([org_a, org_b])
        await setup.flush()
        brand_a = Brand(
            organization_id=org_a.id,
            name="Pool Brand A",
            industry="retail",
            city="Riyadh",
            website=None,
            guidelines={},
        )
        brand_b = Brand(
            organization_id=org_b.id,
            name="Pool Brand B",
            industry="retail",
            city="Jeddah",
            website=None,
            guidelines={},
        )
        setup.add_all([brand_a, brand_b])
        await setup.commit()
        org_a_id, org_b_id = org_a.id, org_b.id
        brand_a_id, brand_b_id = brand_a.id, brand_b.id

    results = await asyncio.gather(
        _pick_and_pin(org_a_id, brand_a_id),
        _pick_and_pin(org_b_id, brand_b_id),
    )
    assert len(results) == 2
    assert results[0] != results[1], (
        "fill_to_tier=1 should spread the second profile to another credential"
    )

    async with factory() as cleanup:
        extras = (
            await cleanup.execute(
                select(ZernioCredential).where(
                    ZernioCredential.alias.notin_(("t1", "t2", "t3"))
                )
            )
        ).scalars().all()
        for extra in extras:
            extra.status = "active"
        await cleanup.commit()


@pytest.mark.asyncio
async def test_select_for_new_profile_returns_active(
    db_session: AsyncSession,
) -> None:
    cred = await select_for_new_profile(db_session, fill_to_tier=3)
    assert cred.status == "active"
    assert cred.alias.startswith("t")


@pytest.mark.asyncio
async def test_select_for_connect_skips_full_keys(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(
        update(ZernioCredential).values(connected_accounts=2, max_accounts=2)
    )
    await db_session.flush()

    # Leave one key with spare slots.
    t3 = (
        await db_session.execute(
            select(ZernioCredential).where(ZernioCredential.alias == "t3")
        )
    ).scalar_one()
    t3.connected_accounts = 0
    await db_session.flush()

    picked = await select_for_connect(db_session, fill_to_tier=3)
    assert picked.alias == "t3"


@pytest.mark.asyncio
async def test_select_for_connect_exhausted(
    db_session: AsyncSession,
) -> None:
    await db_session.execute(
        update(ZernioCredential).values(connected_accounts=2, max_accounts=2)
    )
    await db_session.flush()

    with pytest.raises(CredentialPoolExhausted):
        await select_for_connect(db_session, fill_to_tier=3)
