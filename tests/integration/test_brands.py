"""Phase 4 brand / strategy / cultural-calendar integration tests."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.features.brands.models import BrandVersion, ContentPillar
from tests.db_fixtures import SeedMember


def _login(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def _create_brand(
    client: AsyncClient, org_id: uuid.UUID, name: str = "Noor Clinic"
) -> dict[str, Any]:
    response = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={"name": name, "industry": "healthcare_clinic", "city": "Riyadh"},
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


async def test_create_generate_edit_delete_walkthrough(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _login(client, raw_token)

    created = await _create_brand(client, org_id)
    brand_id = created["id"]
    assert created["version"] == 1
    assert len(created["pillars"]) == 1
    assert created["guidelines"]["dialect"] == "gulf"

    gen = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/generate",
        json={
            "name": "Noor Clinic",
            "industry": "healthcare_clinic",
            "city": "Riyadh",
            "dialect": "msa",
        },
        headers={"Idempotency-Key": f"brand-generate:{brand_id}"},
    )
    assert gen.status_code == 200, gen.text
    generated = gen.json()
    assert generated["version"] == 2
    assert generated["guidelines"]["dialect"] == "msa"
    assert len(generated["pillars"]) == 3

    # Stale version → VERSION_CONFLICT
    stale = await client.patch(
        f"/v1/orgs/{org_id}/brands/{brand_id}",
        json={**generated, "version": 1, "name": "Stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"
    assert stale.json()["error"]["details"]["currentVersion"] == 2

    # Fresh edit succeeds
    edited = await client.patch(
        f"/v1/orgs/{org_id}/brands/{brand_id}",
        json={**generated, "name": "Noor Dental"},
    )
    assert edited.status_code == 200
    assert edited.json()["version"] == 3
    assert edited.headers.get("etag") == '"3"'

    deleted = await client.delete(f"/v1/orgs/{org_id}/brands/{brand_id}")
    assert deleted.status_code == 204

    listed = await client.get(f"/v1/orgs/{org_id}/brands")
    assert listed.status_code == 200
    assert listed.json() == []

    # Pillars are soft-deleted, not hard-deleted.
    pillars = (
        await db_session.execute(
            select(ContentPillar).where(ContentPillar.brand_id == uuid.UUID(brand_id))
        )
    ).scalars().all()
    assert pillars
    assert all(p.deleted_at is not None for p in pillars)


async def test_name_collision_on_create(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)
    await _create_brand(client, org_id, name="Acme")
    conflict = await client.post(
        f"/v1/orgs/{org_id}/brands",
        json={"name": "acme", "industry": "retail", "city": "Jeddah"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CONFLICT"


async def test_generate_idempotency_replay(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)
    created = await _create_brand(client, org_id)
    brand_id = created["id"]
    key = f"brand-generate:{brand_id}"
    body = {
        "name": "Noor Clinic",
        "industry": "restaurant",
        "city": "Riyadh",
    }

    first = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/generate",
        json=body,
        headers={"Idempotency-Key": key},
    )
    assert first.status_code == 200
    second = await client.post(
        f"/v1/orgs/{org_id}/brands/{brand_id}/generate",
        json=body,
        headers={"Idempotency-Key": key},
    )
    assert second.status_code == 200
    assert second.json() == first.json()

    versions = (
        await db_session.execute(
            select(BrandVersion).where(BrandVersion.brand_id == uuid.UUID(brand_id))
        )
    ).scalars().all()
    # create wrote one (edited), generate wrote one (generated) — replay adds none.
    generated_rows = [v for v in versions if v.reason == "generated"]
    assert len(generated_rows) == 1


async def test_concurrent_patch_version_cas(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)
    created = await _create_brand(client, org_id)
    brand_id = created["id"]

    async def _patch(name: str) -> int:
        response = await client.patch(
            f"/v1/orgs/{org_id}/brands/{brand_id}",
            json={**created, "name": name},
        )
        return response.status_code

    results = await asyncio.gather(_patch("Alpha"), _patch("Beta"))
    assert sorted(results) == [200, 409]


async def test_strategy_round_trip(client: AsyncClient, seed_member: SeedMember) -> None:
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)
    created = await _create_brand(client, org_id)
    brand_id = created["id"]

    got = await client.get(f"/v1/orgs/{org_id}/brands/{brand_id}/strategy")
    assert got.status_code == 200
    assert got.json()["goals"] == []

    goal_id = str(uuid.uuid4())
    patched = await client.patch(
        f"/v1/orgs/{org_id}/brands/{brand_id}/strategy",
        json={
            "goals": [{"id": goal_id, "text": "Grow reach", "progress": 10}],
            "cadence": {"instagram": 4},
        },
    )
    assert patched.status_code == 200
    assert patched.json()["goals"][0]["text"] == "Grow reach"
    assert patched.json()["cadence"]["instagram"] == 4


async def test_cultural_events_toggle_is_org_scoped(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_a, _ua, token_a = await seed_member(role="editor")
    org_b, _ub, token_b = await seed_member(role="editor")

    _login(client, token_a)
    list_a = await client.get(f"/v1/orgs/{org_a}/cultural-events")
    assert list_a.status_code == 200
    events = list_a.json()
    assert len(events) >= 10
    event_id = events[0]["id"]
    was_enabled = events[0]["enabled"]

    toggled = await client.post(f"/v1/orgs/{org_a}/cultural-events/{event_id}/toggle")
    assert toggled.status_code == 200
    assert toggled.json()["enabled"] is (not was_enabled)

    _login(client, token_b)
    list_b = await client.get(f"/v1/orgs/{org_b}/cultural-events")
    other = next(e for e in list_b.json() if e["id"] == event_id)
    assert other["enabled"] is was_enabled


async def test_editor_cannot_delete_brand(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _login(client, raw_token)
    created = await _create_brand(client, org_id)
    response = await client.delete(f"/v1/orgs/{org_id}/brands/{created['id']}")
    assert response.status_code == 403


async def test_oauth_states_brand_fk_rejects_unknown_brand(
    db_session: AsyncSession, seed_member: SeedMember
) -> None:
    """A missing ADD CONSTRAINT step does not fail alembic upgrade — this does."""
    org_id, user_id, _token = await seed_member(role="owner")
    session_row = (
        await db_session.execute(
            text("SELECT id FROM sessions WHERE user_id = :uid LIMIT 1"),
            {"uid": user_id},
        )
    ).one()

    bogus_brand = uuid.uuid4()
    with pytest.raises(Exception):  # noqa: B017 — IntegrityError via asyncpg
        await db_session.execute(
            text(
                """
                INSERT INTO oauth_states (
                    state_hash, organization_id, brand_id, platform,
                    user_id, session_id, expires_at
                ) VALUES (
                    :hash, :org, :brand, 'instagram', :user, :session, now() + interval '10 minutes'
                )
                """
            ),
            {
                "hash": "c" * 64,
                "org": org_id,
                "brand": bogus_brand,
                "user": user_id,
                "session": session_row[0],
            },
        )
        await db_session.flush()
