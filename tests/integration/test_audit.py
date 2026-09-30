from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.audit import record
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def test_list_audit_log_returns_newest_first(
    client: AsyncClient, db_session: AsyncSession, seed_member: SeedMember
) -> None:
    org_id, user_id, raw_token = await seed_member(role="admin")
    for action in ("org.created", "team.invited", "settings.frozen"):
        await record(
            db_session,
            organization_id=org_id,
            actor_kind="user",
            actor_ref=str(user_id),
            actor_name="Seeded User",
            action=action,
            target_type="organization",
            target_id=org_id,
        )
    await db_session.commit()
    _login(client, raw_token)

    response = await client.get(f"/v1/orgs/{org_id}/audit")

    assert response.status_code == 200
    body = response.json()
    actions = [item["action"] for item in body["items"]]
    assert actions == ["settings.frozen", "team.invited", "org.created"]
    # `response_model_exclude_none=True`: the key is absent on the last page,
    # not present with a `null` value — architecture/04's `meta`/`nextCursor`
    # fields are optional-on-the-wire, not nullable.
    assert body.get("nextCursor") is None


async def test_audit_log_is_tenant_scoped(
    client: AsyncClient, db_session: AsyncSession, seed_member: SeedMember
) -> None:
    org_a, user_a, token_a = await seed_member(role="admin")
    org_b, user_b, _token_b = await seed_member(role="admin")
    await record(
        db_session,
        organization_id=org_a,
        actor_kind="user",
        actor_ref=str(user_a),
        actor_name="A",
        action="org.created",
        target_type="organization",
        target_id=org_a,
    )
    await record(
        db_session,
        organization_id=org_b,
        actor_kind="user",
        actor_ref=str(user_b),
        actor_name="B",
        action="org.created",
        target_type="organization",
        target_id=org_b,
    )
    await db_session.commit()
    _login(client, token_a)

    response = await client.get(f"/v1/orgs/{org_a}/audit")

    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


async def test_audit_log_pagination_follows_the_cursor(
    client: AsyncClient, db_session: AsyncSession, seed_member: SeedMember
) -> None:
    org_id, user_id, raw_token = await seed_member(role="admin")
    for i in range(25):
        await record(
            db_session,
            organization_id=org_id,
            actor_kind="user",
            actor_ref=str(user_id),
            actor_name="Seeded User",
            action=f"test.action.{i}",
            target_type="organization",
            target_id=org_id,
        )
    await db_session.commit()
    _login(client, raw_token)

    first_page = (await client.get(f"/v1/orgs/{org_id}/audit")).json()
    assert len(first_page["items"]) == 20
    assert first_page["nextCursor"] is not None

    second_page = (
        await client.get(f"/v1/orgs/{org_id}/audit", params={"cursor": first_page["nextCursor"]})
    ).json()
    assert len(second_page["items"]) == 5
    assert second_page.get("nextCursor") is None

    first_ids = {item["id"] for item in first_page["items"]}
    second_ids = {item["id"] for item in second_page["items"]}
    assert first_ids.isdisjoint(second_ids)
