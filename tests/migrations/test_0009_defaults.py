"""Server-default smoke tests for brand_research_runs."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.features.auth.models import User
from app.features.brand_research.models import BrandResearchRun
from app.features.brands.models import Brand
from app.features.organizations.models import Organization


async def _seed(db_session: AsyncSession) -> tuple[Organization, Brand, User]:
    user = User(email="research-defaults@example.com", name="Research User")
    org = Organization(name="Research Org")
    db_session.add_all([user, org])
    await db_session.flush()
    brand = Brand(
        organization_id=org.id,
        name="Research Brand",
        industry="retail",
        city="Riyadh",
        website="https://example.sa",
        guidelines={},
    )
    db_session.add(brand)
    await db_session.flush()
    return org, brand, user


async def test_brand_research_runs_defaults(db_session: AsyncSession) -> None:
    org, brand, user = await _seed(db_session)
    row = BrandResearchRun(
        organization_id=org.id,
        brand_id=brand.id,
        requested_by=user.id,
        source_url="https://example.sa",
    )
    db_session.add(row)
    await db_session.flush()
    assert row.status == "queued"
    assert row.id is not None
    assert row.created_at is not None
    assert row.updated_at is not None
    assert row.extracted is None
    assert row.proposal is None
    assert row.decision_id is None
    assert row.crawled_pages is None
