from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import install_exception_handlers
from app.core.request_context import (
    AccessContext,
    require_capability,
    require_membership,
    require_min_role,
)
from tests.db_fixtures import SeedMember


def _build_app() -> FastAPI:
    app = FastAPI()
    install_exception_handlers(app)

    @app.get("/orgs/{orgId}/whoami")
    async def whoami(ctx: Annotated[AccessContext, Depends(require_membership)]) -> dict[str, str]:
        return {"role": ctx.role, "userId": str(ctx.user_id)}

    @app.get("/orgs/{orgId}/needs-settings-manage")
    async def needs_cap(
        ctx: Annotated[AccessContext, Depends(require_capability("settings.manage"))],
    ) -> dict[str, str]:
        return {"role": ctx.role}

    @app.get("/orgs/{orgId}/needs-admin")
    async def needs_admin(
        ctx: Annotated[AccessContext, Depends(require_min_role("admin"))],
    ) -> dict[str, str]:
        return {"role": ctx.role}

    return app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="https://test") as ac:
        yield ac


def _set_session_cookie(client: AsyncClient, raw_token: str) -> None:
    client.cookies.set(get_settings().auth.session_cookie_name, raw_token)


async def test_no_session_cookie_is_unauthorized(client: AsyncClient) -> None:
    response = await client.get(f"/orgs/{uuid.uuid4()}/whoami")
    assert response.status_code == 401


async def test_member_can_read_their_own_role(client: AsyncClient, seed_member: SeedMember) -> None:
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _set_session_cookie(client, raw_token)

    response = await client.get(f"/orgs/{org_id}/whoami")

    assert response.status_code == 200
    assert response.json()["role"] == "editor"


async def test_a_valid_session_for_a_different_org_is_not_found(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    # architecture/05 §3: a caller with a real, live session simply isn't a
    # member of *this* org — 404, never 403, so a 403 can't be used to sweep
    # for which org ids are real.
    _org_id, _user_id, raw_token = await seed_member(role="owner")
    _set_session_cookie(client, raw_token)

    response = await client.get(f"/orgs/{uuid.uuid4()}/whoami")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


async def test_capability_check_allows_a_role_that_has_it(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="admin")
    _set_session_cookie(client, raw_token)

    response = await client.get(f"/orgs/{org_id}/needs-settings-manage")
    assert response.status_code == 200


async def test_capability_check_denies_a_role_without_it(
    client: AsyncClient, seed_member: SeedMember
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _set_session_cookie(client, raw_token)

    response = await client.get(f"/orgs/{org_id}/needs-settings-manage")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


async def test_min_role_admin_rejects_editor(client: AsyncClient, seed_member: SeedMember) -> None:
    org_id, _user_id, raw_token = await seed_member(role="editor")
    _set_session_cookie(client, raw_token)

    response = await client.get(f"/orgs/{org_id}/needs-admin")
    assert response.status_code == 403


async def test_min_role_admin_accepts_owner(client: AsyncClient, seed_member: SeedMember) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    _set_session_cookie(client, raw_token)

    response = await client.get(f"/orgs/{org_id}/needs-admin")
    assert response.status_code == 200


async def test_a_revoked_session_is_unauthorized(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    await db_session.execute(
        text("UPDATE sessions SET revoked_at = now() WHERE token_hash = :h"), {"h": token_hash}
    )
    await db_session.commit()
    _set_session_cookie(client, raw_token)

    response = await client.get(f"/orgs/{org_id}/whoami")
    assert response.status_code == 401


async def test_an_expired_session_is_unauthorized(
    client: AsyncClient, seed_member: SeedMember, db_session: AsyncSession
) -> None:
    org_id, _user_id, raw_token = await seed_member(role="owner")
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    await db_session.execute(
        text("UPDATE sessions SET expires_at = now() - interval '1 hour' WHERE token_hash = :h"),
        {"h": token_hash},
    )
    await db_session.commit()
    _set_session_cookie(client, raw_token)

    response = await client.get(f"/orgs/{org_id}/whoami")
    assert response.status_code == 401
