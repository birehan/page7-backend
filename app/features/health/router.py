from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Environment, Settings, get_settings
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


# A queued job this far past its run time means workers are not keeping up.
_JOB_OVERDUE_SECONDS = 600
# The scheduler ticks every few seconds and a per-minute job is registered; no run for this
# long means the scheduler process is down or stuck.
_SCHEDULER_STALE_SECONDS = 300
_SCHEDULER_PROBE_JOB = "reconcile_publications"


@router.get("/deep")
async def deep_health(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> JSONResponse:
    """Is the whole system working, not just this process? For uptime monitors and alerts,
    not load balancers (use /live and /ready for those). Returns 503 when something is wrong.

    - database: reachable, with round-trip time
    - jobQueue: no job has been waiting past its run time for long (workers are draining it)
    - scheduler: its per-minute job ran recently (outside dev, "never ran" counts as a failure)
    """
    checks: dict[str, dict[str, Any]] = {}

    started = time.perf_counter()
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        checks["database"] = {"ok": False}
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "down", "checks": checks},
        )
    checks["database"] = {"ok": True, "latencyMs": round((time.perf_counter() - started) * 1000)}

    queue = (
        await session.execute(
            text(
                "SELECT count(*) AS overdue, "
                "COALESCE(EXTRACT(EPOCH FROM (now() - min(run_at))), 0)::int AS oldest "
                "FROM jobs WHERE state = 'queued' AND run_at < now() - interval '1 minute'"
            )
        )
    ).one()
    checks["jobQueue"] = {
        "ok": queue.oldest <= _JOB_OVERDUE_SECONDS,
        "overdueJobs": int(queue.overdue),
        "oldestOverdueSeconds": int(queue.oldest),
    }

    last_run = (
        await session.execute(
            text(
                "SELECT EXTRACT(EPOCH FROM (now() - max(period_start)))::int "
                "FROM cron_runs WHERE name = :name"
            ),
            {"name": _SCHEDULER_PROBE_JOB},
        )
    ).scalar_one()
    never_ran_is_ok = settings.app_env in (Environment.DEVELOPMENT, Environment.TESTING)
    checks["scheduler"] = {
        "ok": (last_run is None and never_ran_is_ok)
        or (last_run is not None and last_run <= _SCHEDULER_STALE_SECONDS),
        "lastRunSecondsAgo": last_run,
    }

    healthy = all(check["ok"] for check in checks.values())
    return JSONResponse(
        status_code=status.HTTP_200_OK if healthy else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"status": "ok" if healthy else "degraded", "checks": checks},
    )
