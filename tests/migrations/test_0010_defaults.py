"""Server-default smoke tests for Phase 9 social/Zernio tables."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.auth.models import User
from app.features.brands.models import Brand
from app.features.organizations.models import Organization
from app.features.social_accounts.models import (
    SocialAccount,
    WebhookEvent,
    ZernioCredential,
    ZernioProfile,
)


async def _seed(db_session: AsyncSession) -> tuple[Organization, Brand, User]:
    user = User(email="social-defaults@example.com", name="Social User")
    org = Organization(name="Social Org")
    db_session.add_all([user, org])
    await db_session.flush()
    brand = Brand(
        organization_id=org.id,
        name="Social Brand",
        industry="retail",
        city="Riyadh",
        website="https://example.sa",
        guidelines={},
    )
    db_session.add(brand)
    await db_session.flush()
    return org, brand, user


async def test_zernio_credentials_seeded(db_session: AsyncSession) -> None:
    rows = (
        await db_session.execute(
            select(ZernioCredential).where(
                ZernioCredential.alias.in_(("t1", "t2", "t3"))
            )
        )
    ).scalars().all()
    assert len(rows) == 3
    by_alias = {r.alias: r for r in rows}
    for alias in ("t1", "t2", "t3"):
        row = by_alias[alias]
        assert row.status == "active"
        assert row.max_profiles == 100
        assert row.max_accounts >= 2
        assert row.secret_ref == f"ZERNIO_API_KEY__{alias}"
        assert row.connected_accounts >= 0
        assert row.id is not None
        assert row.created_at is not None


async def test_social_accounts_defaults(db_session: AsyncSession) -> None:
    org, brand, _user = await _seed(db_session)
    row = SocialAccount(
        organization_id=org.id,
        brand_id=brand.id,
        platform="instagram",
    )
    db_session.add(row)
    await db_session.flush()
    assert row.status == "disconnected"
    assert row.handle == ""
    assert row.id is not None
    assert row.created_at is not None
    assert row.updated_at is not None
    assert row.zernio_profile_id is None
    assert row.credential_id is None
    assert row.scopes is None


async def test_webhook_events_defaults(db_session: AsyncSession) -> None:
    row = WebhookEvent(
        provider="zernio",
        external_event_id="evt_defaults_1",
        event_type="account.connected",
        payload={"ok": True},
        signature_valid=True,
    )
    db_session.add(row)
    await db_session.flush()
    assert row.attempts == 0
    assert row.received_at is not None
    assert row.id is not None
    assert row.processed_at is None


async def test_social_account_pin_mismatch_rejected(db_session: AsyncSession) -> None:
    """Composite FK rejects profile A paired with a different credential."""
    org, brand, _user = await _seed(db_session)
    creds = (
        await db_session.execute(
            select(ZernioCredential).where(
                ZernioCredential.alias.in_(("t1", "t2"))
            )
        )
    ).scalars().all()
    by_alias = {c.alias: c for c in creds}
    cred_a = by_alias["t1"]
    cred_b = by_alias["t2"]

    profile = ZernioProfile(
        organization_id=org.id,
        brand_id=brand.id,
        credential_id=cred_a.id,
        zernio_profile_id="zp_pin_test",
    )
    db_session.add(profile)
    await db_session.flush()

    mismatched = SocialAccount(
        organization_id=org.id,
        brand_id=brand.id,
        platform="facebook",
        zernio_profile_id=profile.id,
        credential_id=cred_b.id,
        zernio_account_id="za_mismatch",
        status="connected",
    )
    db_session.add(mismatched)
    with pytest.raises(IntegrityError):
        await db_session.flush()
