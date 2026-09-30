from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db_session

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live", status_code=status.HTTP_200_OK)
async def liveness() -> dict[str, str]:
    """The process is up and serving requests. Never checks a dependency —
    a database outage must not make the load balancer kill healthy processes.
    """
    return {"status": "ok"}


@router.get("/ready", status_code=status.HTTP_200_OK)
async def readiness(session: Annotated[AsyncSession, Depends(get_db_session)]) -> dict[str, str]:
    """The process can serve real traffic: the database is reachable."""
    await session.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.get("/deep", status_code=status.HTTP_200_OK)
async def deep_health() -> dict[str, Any]:
    """Provider connectivity checks. Checks nothing in Phase 1 — no provider
    credential exists in-app until Phase 2's Resend integration; each phase that
    adds a provider adapter adds its check here.
    """
    return {"status": "ok", "checks": {}}
