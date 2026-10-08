"""Audit rows carry the caller's IP and request id; only admins can read the log."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.audit.models import AuditLog
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _freeze_row(db_session: AsyncSession, org_id: object) -> AuditLog:
    return (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.organization_id == org_id, AuditLog.action == "publishing.frozen"
            )
        )
    ).scalar_one()


async def test_audit_row_records_request_id_and_ip(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    response = await client.post(
        f"/v1/orgs/{org_id}/settings/freeze",
        json={},
        headers={"X-Request-Id": "test-request-id-0001"},
    )
    assert response.status_code == 200

    row = await _freeze_row(db_session, org_id)
    assert row.request_id == "test-request-id-0001"
    assert row.ip is not None


async def test_malformed_request_id_is_replaced_not_stored(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)
    hostile = 'abc "quoted" {json: injection}'

    response = await client.post(
        f"/v1/orgs/{org_id}/settings/freeze", json={}, headers={"X-Request-Id": hostile}
    )

    assert response.status_code == 200
    echoed = response.headers["X-Request-Id"]
    assert echoed != hostile
    row = await _freeze_row(db_session, org_id)
    assert row.request_id == echoed


@pytest.mark.parametrize("role", ["viewer", "editor", "approver"])
async def test_non_admin_cannot_read_the_audit_log(
    client: AsyncClient, seed_member: SeedMember, role: str
) -> None:
    org_id, _user_id, raw_token = await seed_member(role=role)
    _login(client, raw_token)
    response = await client.get(f"/v1/orgs/{org_id}/audit")
    assert response.status_code == 403


@pytest.mark.parametrize("role", ["admin", "owner"])
async def test_admin_and_owner_can_read_the_audit_log(
    client: AsyncClient, seed_member: SeedMember, role: str
) -> None:
    org_id, _user_id, raw_token = await seed_member(role=role)
    _login(client, raw_token)
    response = await client.get(f"/v1/orgs/{org_id}/audit")
    assert response.status_code == 200
