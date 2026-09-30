"""DELETE /orgs/:orgId — soft-delete + 30-day purge enqueue (Phase 15)."""

from __future__ import annotations

from datetime import timedelta

from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.time import utc_now
from app.features.organizations.models import Organization
from app.features.organizations.tasks import handle_purge_organization
from app.jobs.models import Job
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def test_owner_can_soft_delete_organization(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    response = await client.delete(f"/v1/orgs/{org_id}")
    assert response.status_code == 204

    org = await db_session.get(Organization, org_id)
    assert org is not None
    assert org.deleted_at is not None
    assert org.deleted_by == user_id

    job = (
        await db_session.execute(
            select(Job).where(Job.unique_key == f"purge_org:{org_id}")
        )
    ).scalar_one()
    assert job.type == "maintenance.purge_organization"
    assert job.queue == "maintenance"
    assert job.run_at >= utc_now() + timedelta(days=29)


async def test_delete_organization_is_idempotent(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    assert (await client.delete(f"/v1/orgs/{org_id}")).status_code == 204
    first = (
        await db_session.execute(
            select(Job).where(Job.unique_key == f"purge_org:{org_id}")
        )
    ).scalar_one()
    first_run_at = first.run_at

    assert (await client.delete(f"/v1/orgs/{org_id}")).status_code == 204
    jobs = (
        await db_session.execute(
            select(Job).where(Job.unique_key == f"purge_org:{org_id}")
        )
    ).scalars().all()
    assert len(jobs) == 1
    assert jobs[0].run_at == first_run_at


async def test_admin_cannot_delete_organization(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="admin")
    _login(client, raw_token)
    response = await client.delete(f"/v1/orgs/{org_id}")
    assert response.status_code == 403


async def test_cross_tenant_delete_returns_404(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    _org_a, _user_a, token_a = await seed_member(role="owner")
    org_b, _user_b, _token_b = await seed_member(role="owner")
    _login(client, token_a)

    response = await client.delete(f"/v1/orgs/{org_b}")
    assert response.status_code == 404


async def test_purge_organization_anonymizes_audit_and_retains_invoices(
    seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, user_id, _token = await seed_member(role="owner")

    await db_session.execute(
        text(
            """
            INSERT INTO audit_logs
                (id, organization_id, actor_kind, actor_user_id, actor_ref, actor_name, action,
                 target_type, target_id, ip, meta)
            VALUES
                (uuidv7(), :org_id, 'user', :user_id, :actor_ref, 'Alice',
                 'org.settings.updated',
                 'organization', :org_id, '203.0.113.10', '{"k":"v"}'::jsonb)
            """
        ),
        {"org_id": org_id, "user_id": user_id, "actor_ref": str(user_id)},
    )
    invoice_number = f"INV-PHASE15-{org_id.hex[:8]}"
    await db_session.execute(
        text(
            """
            INSERT INTO invoices
                (id, organization_id, number, status, currency,
                 amount_subtotal, vat_rate, vat_amount, total,
                 seller_vat_number, period_start, period_end, issued_at)
            VALUES
                (uuidv7(), :org_id, :number, 'paid', 'SAR',
                 100, 0.15, 15, 115,
                 '300000000000003', CURRENT_DATE, CURRENT_DATE, now())
            """
        ),
        {"org_id": org_id, "number": invoice_number},
    )
    await db_session.commit()

    await handle_purge_organization({"organization_id": str(org_id)})

    audit_row = (
        await db_session.execute(
            text(
                """
                SELECT actor_name, ip, actor_user_id, meta
                FROM audit_logs WHERE organization_id = :org_id
                """
            ),
            {"org_id": org_id},
        )
    ).one()
    assert audit_row.actor_name == "[redacted]"
    assert audit_row.ip is None
    assert audit_row.actor_user_id is None

    invoice_count = (
        await db_session.execute(
            text("SELECT count(*) FROM invoices WHERE organization_id = :org_id"),
            {"org_id": org_id},
        )
    ).scalar_one()
    assert invoice_count == 1

    org = await db_session.get(Organization, org_id)
    assert org is not None
