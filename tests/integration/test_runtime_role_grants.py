"""psql-level grants: pgblank_runtime cannot DELETE/UPDATE audit_logs."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_runtime_role_denied_audit_log_mutation_when_role_exists(
    db_session: AsyncSession,
) -> None:
    """architecture/05 §11 / checklist §1 — deny UPDATE/DELETE on audit_logs.

    Applied against the local test database when the Phase 15 roles have been
    created (infra/gcp/sql/roles.sql). Skips cleanly when roles are absent so
    CI without the bootstrap still passes.
    """
    row = (
        await db_session.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = 'pgblank_runtime'")
        )
    ).first()
    if row is None:
        pytest.skip("pgblank_runtime role not present (apply infra/gcp/sql/roles.sql)")

    # SET ROLE only works if the current login is a member of pgblank_runtime
    # or is a superuser (local test user is typically a superuser).
    await db_session.execute(text("SET LOCAL ROLE pgblank_runtime"))
    with pytest.raises(Exception) as exc_info:
        await db_session.execute(text("DELETE FROM audit_logs WHERE false"))
    await db_session.rollback()
    msg = str(exc_info.value).lower()
    assert "permission" in msg or "privilege" in msg or "denied" in msg
