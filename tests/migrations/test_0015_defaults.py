"""Server-default smoke for 0015 zernio credential capacity."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.auth.models import User
from app.features.brands.models import Brand
from app.features.organizations.models import Organization
from app.features.social_accounts.models import ZernioCredential, ZernioProfile


@pytest.mark.asyncio
async def test_0015_max_accounts_column_default(db_session: AsyncSession) -> None:
    row = (
        await db_session.execute(
            text(
                """
                SELECT column_default
                FROM information_schema.columns
                WHERE table_name = 'zernio_credentials'
                  AND column_name = 'max_accounts'
                """
            )
        )
    ).one()
    assert "2" in str(row[0])


@pytest.mark.asyncio
async def test_0015_multi_profile_per_brand_index(
    db_session: AsyncSession,
) -> None:
    """Two live profiles on different credentials for one brand are allowed."""
    user = User(email="cap-multi@example.com", name="Cap")
    org = Organization(name="Cap Org")
    db_session.add_all([user, org])
    await db_session.flush()
    brand = Brand(
        organization_id=org.id,
        name="Cap Brand",
        industry="retail",
        city="Riyadh",
        website="https://example.sa",
        guidelines={},
    )
    db_session.add(brand)
    await db_session.flush()

    t1 = (
        await db_session.execute(
            select(ZernioCredential).where(ZernioCredential.alias == "t1")
        )
    ).scalar_one()
    t2 = (
        await db_session.execute(
            select(ZernioCredential).where(ZernioCredential.alias == "t2")
        )
    ).scalar_one()

    db_session.add(
        ZernioProfile(
            organization_id=org.id,
            brand_id=brand.id,
            credential_id=t1.id,
            zernio_profile_id="zp_cap_t1",
        )
    )
    db_session.add(
        ZernioProfile(
            organization_id=org.id,
            brand_id=brand.id,
            credential_id=t2.id,
            zernio_profile_id="zp_cap_t2",
            created_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
