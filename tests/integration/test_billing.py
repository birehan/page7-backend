"""Phase 13 billing integration — endpoints, limits, concurrency, idempotency."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import utc_now
from app.db.session import get_session_factory
from app.features.billing import repository, service
from app.features.content_ai.models import AiUsageCounter
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


@pytest.mark.asyncio
async def test_get_subscription_and_invoices(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    sub = await client.get(f"/v1/orgs/{org_id}/billing/subscription")
    assert sub.status_code == 200, sub.text
    body = sub.json()
    assert body["plan"] == "Growth"
    assert body["status"] in {"trialing", "active", "past_due"}
    assert body["priceMonthly"] == 1499
    assert "renewsAt" in body

    invoices = await client.get(f"/v1/orgs/{org_id}/billing/invoices")
    assert invoices.status_code == 200
    assert invoices.json() == []


@pytest.mark.asyncio
async def test_change_plan_issues_pending_invoice(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="admin")
    _login(client, raw_token)

    key = f"billing-plan:{org_id}:{uuid.uuid4()}"
    patched = await client.patch(
        f"/v1/orgs/{org_id}/billing/subscription",
        json={"plan": "Agency"},
        headers={"Idempotency-Key": key},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["plan"] == "Agency"
    assert patched.json()["priceMonthly"] == 3499

    invoices = await client.get(f"/v1/orgs/{org_id}/billing/invoices")
    assert invoices.status_code == 200
    rows = invoices.json()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["amount"] == 3499
    assert rows[0]["number"].startswith("INV-")


@pytest.mark.asyncio
async def test_change_plan_idempotency_three_cases(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    key = f"billing-plan:{org_id}:idem"

    first = await client.patch(
        f"/v1/orgs/{org_id}/billing/subscription",
        json={"plan": "Starter"},
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 200, first.text

    # Identical replay
    second = await client.patch(
        f"/v1/orgs/{org_id}/billing/subscription",
        json={"plan": "Starter"},
        headers={"Idempotency-Key": key},
    )
    assert second.status_code == 200
    assert second.json() == first.json()

    # Same key, different body → mismatch
    mismatch = await client.patch(
        f"/v1/orgs/{org_id}/billing/subscription",
        json={"plan": "Agency"},
        headers={"Idempotency-Key": key},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "IDEMPOTENCY_MISMATCH"

    # Missing key → validation
    missing = await client.patch(
        f"/v1/orgs/{org_id}/billing/subscription",
        json={"plan": "Growth"},
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "VALIDATION"


@pytest.mark.asyncio
async def test_editor_cannot_change_plan(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)
    response = await client.patch(
        f"/v1/orgs/{org_id}/billing/subscription",
        json={"plan": "Agency"},
        headers={"Idempotency-Key": f"billing:{uuid.uuid4()}"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_brand_limit_blocks_fourth_on_growth(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    for i in range(3):
        ok = await client.post(
            f"/v1/orgs/{org_id}/brands",
            json={
                "name": f"Brand {i} {uuid.uuid4().hex[:4]}",
                "industry": "retail",
                "city": "Riyadh",
            },
        )
        assert ok.status_code == 201, ok.text

    blocked = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"Brand Over {uuid.uuid4().hex[:4]}",
            "industry": "retail",
            "city": "Riyadh",
        },
    )
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "PLAN_LIMIT_BRANDS"


@pytest.mark.asyncio
async def test_brand_limit_race_exactly_one_succeeds(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    """Two concurrent creates at brands_limit-1 → exactly one succeeds."""
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    # Drop to starter (limit 1) and ensure zero brands.
    sub = (
        await db_session.execute(
            text(
                "SELECT id FROM subscriptions WHERE organization_id = :oid "
                "AND status IN ('trialing','active','past_due')"
            ),
            {"oid": org_id},
        )
    ).scalar_one()
    await db_session.execute(
        text("UPDATE subscriptions SET plan_code = 'starter' WHERE id = :id"),
        {"id": sub},
    )
    await db_session.commit()

    async def _create(name: str) -> int:
        response = await client.post(
            f"/v1/orgs/{org_id}/brands",
            json={"name": name, "industry": "retail", "city": "Riyadh"},
        )
        return response.status_code

    results = await asyncio.gather(
        _create(f"Race A {uuid.uuid4().hex[:6]}"),
        _create(f"Race B {uuid.uuid4().hex[:6]}"),
    )
    assert sorted(results) == [201, 403]


@pytest.mark.asyncio
async def test_invoice_counters_gapless_under_concurrency(
    seed_member: SeedMember,
) -> None:
    org_id, _user_id, _token = await seed_member(role="owner")
    settings = get_settings()

    async def _issue(i: int) -> str:
        async with get_session_factory()() as session:
            sub = await repository.get_live_subscription(
                session, organization_id=org_id
            )
            assert sub is not None
            now = utc_now()
            invoice = await service.issue_invoice(
                session,
                organization_id=org_id,
                subscription_id=sub.id,
                subtotal=Decimal("10.00"),
                period_start=now,
                period_end=now + timedelta(days=30),
                settings=settings,
            )
            await session.commit()
            return invoice.number

    numbers = await asyncio.gather(*[_issue(i) for i in range(8)])
    assert len(numbers) == len(set(numbers))
    # Consecutive suffixes within this batch (global counter may have prior values).
    suffixes = sorted(int(n.split("-")[-1]) for n in numbers)
    assert suffixes == list(range(suffixes[0], suffixes[0] + len(suffixes)))


@pytest.mark.asyncio
async def test_ai_usage_matches_counter(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    period = datetime.now(UTC).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    db_session.add(
        AiUsageCounter(
            organization_id=org_id,
            period_month=period.date(),
            generations=7,
        )
    )
    await db_session.commit()

    response = await client.get(
        f"/v1/orgs/{org_id}/billing/ai-usage",
        params={"since": period.isoformat().replace("+00:00", "Z")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["used"] == 7


@pytest.mark.asyncio
async def test_ai_credit_exhaustion_blocks_captions(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    brand = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={
            "name": f"AI Cap {uuid.uuid4().hex[:6]}",
            "industry": "retail",
            "city": "Riyadh",
        },
    )
    assert brand.status_code == 201, brand.text
    brand_id = brand.json()["id"]

    # Force starter AI limit and fill the counter.
    await db_session.execute(
        text(
            "UPDATE subscriptions SET plan_code = 'starter' "
            "WHERE organization_id = :oid AND status IN ('trialing','active','past_due')"
        ),
        {"oid": org_id},
    )
    period = repository.period_month_of(utc_now())
    db_session.add(
        AiUsageCounter(
            organization_id=org_id,
            period_month=period,
            generations=200,
        )
    )
    await db_session.commit()

    response = await client.post(
        "/v1/ai/captions",
        json={
            "brandId": brand_id,
            "intent": "generate",
            "platforms": ["instagram"],
            "dialect": "gulf",
        },
    )
    assert response.status_code == 429, response.text
    assert response.json()["error"]["code"] == "AI_CREDITS_EXHAUSTED"


@pytest.mark.asyncio
async def test_ai_credit_race_overshoot_is_bounded(
    db_session: AsyncSession, seed_member: SeedMember
) -> None:
    """Concurrent credit checks may overshoot by a small bounded amount."""
    org_id, _user_id, _token = await seed_member(role="owner")
    await db_session.execute(
        text(
            "UPDATE subscriptions SET plan_code = 'starter' "
            "WHERE organization_id = :oid AND status IN ('trialing','active','past_due')"
        ),
        {"oid": org_id},
    )
    period = repository.period_month_of(utc_now())
    db_session.add(
        AiUsageCounter(
            organization_id=org_id,
            period_month=period,
            generations=199,
        )
    )
    await db_session.commit()

    async def _check() -> str:
        async with get_session_factory()() as session:
            try:
                await service.enforce_ai_credit_limit(
                    session, organization_id=org_id
                )
                # Simulate a successful generation write.
                await session.execute(
                    text(
                        """
                        INSERT INTO ai_usage_counters
                          (organization_id, period_month, generations)
                        VALUES (:oid, :pm, 1)
                        ON CONFLICT (organization_id, period_month) DO UPDATE
                          SET generations = ai_usage_counters.generations + 1
                        """
                    ),
                    {"oid": org_id, "pm": period},
                )
                await session.commit()
                return "ok"
            except Exception as exc:  # noqa: BLE001
                from app.core.errors import ApiError

                if isinstance(exc, ApiError) and exc.code == "AI_CREDITS_EXHAUSTED":
                    return "blocked"
                raise

    results = await asyncio.gather(*[_check() for _ in range(4)])
    assert "ok" in results
    # At most a small overshoot — never more than concurrency window.
    async with get_session_factory()() as session:
        used = await repository.get_usage_generations(
            session, organization_id=org_id, period_month=period
        )
    assert used <= 199 + 4
    assert used >= 200
