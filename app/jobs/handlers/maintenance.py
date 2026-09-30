"""Maintenance sweep handlers (architecture/12 §Backend components).

Four handlers for the periodic cleanup jobs the cron scheduler fires:
- cleanup_sessions     — delete expired sessions (past expires_at + 7 days)
- expire_idempotency_keys — purge expired idempotency keys (via IdempotencyStore)
- prune_rate_limits    — delete rate-limit windows older than 1 day
- prune_jobs           — prune terminal job rows by state-specific retention
- purge_run_events     — delete run_events rows >24h after their run finished

All handlers run on the RUNTIME connection (pgblank_retention role split is
deferred — see docs/phases/phase-03-background-jobs.md §Decisions).

Each handler deletes in small LIMIT-bounded batches with a short sleep between
batches so a large prune never holds a long transaction against a hot table
(architecture/02 §9 and architecture/12 §Backend components).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session_factory

log = structlog.get_logger(__name__)

_BATCH = 500       # rows per DELETE batch
_SLEEP = 0.05      # seconds between batches (reduces lock pressure)


async def _batch_delete(
    session: AsyncSession, sql: str, params: dict[str, Any], label: str
) -> int:
    """Run the given DELETE SQL in LIMIT-bounded batches."""
    total = 0
    while True:
        result: CursorResult[Any] = await session.execute(text(sql), params)  # type: ignore[assignment]
        await session.commit()
        n: int = result.rowcount
        total += n
        if n < _BATCH:
            break
        await asyncio.sleep(_SLEEP)
    if total:
        log.info(f"maintenance.{label}", deleted=total)
    return total


# ---------------------------------------------------------------------------
# cleanup_sessions — hourly
# ---------------------------------------------------------------------------

async def handle_cleanup_sessions(payload: dict[str, Any]) -> None:
    """Delete sessions past expires_at + 7 days (retention = expired + 7d).

    architecture/02 §1: uses ix_sessions_expiry (expires_at).
    """
    cutoff = datetime.now(UTC) - timedelta(days=7)
    async with get_session_factory()() as session:
        await _batch_delete(
            session,
            sql="""
                DELETE FROM sessions
                WHERE id IN (
                    SELECT id FROM sessions
                    WHERE expires_at < :cutoff
                    LIMIT :batch
                )
            """,
            params={"cutoff": cutoff, "batch": _BATCH},
            label="cleanup_sessions",
        )


# ---------------------------------------------------------------------------
# expire_idempotency_keys — every 30 minutes
# ---------------------------------------------------------------------------

async def handle_expire_idempotency_keys(payload: dict[str, Any]) -> None:
    """Purge expired idempotency keys (architecture/02 §9: 24h retention).

    Delegates to IdempotencyStore.purge_expired() — the call site Phase 3's
    spec says is "already there waiting for this phase."
    """
    from app.infrastructure.idempotency.store import IdempotencyStore

    async with get_session_factory()() as session:
        store = IdempotencyStore(session)
        n = await store.purge_expired()
        await session.commit()
    if n:
        log.info("maintenance.expire_idempotency_keys", deleted=n)


# ---------------------------------------------------------------------------
# prune_rate_limits — daily
# ---------------------------------------------------------------------------

async def handle_prune_rate_limits(payload: dict[str, Any]) -> None:
    """Delete rate_limit windows older than 1 day."""
    cutoff = datetime.now(UTC) - timedelta(days=1)
    async with get_session_factory()() as session:
        await _batch_delete(
            session,
            sql="""
                DELETE FROM rate_limits
                WHERE (bucket, window_start) IN (
                    SELECT bucket, window_start FROM rate_limits
                    WHERE window_start < :cutoff
                    LIMIT :batch
                )
            """,
            params={"cutoff": cutoff, "batch": _BATCH},
            label="prune_rate_limits",
        )


# ---------------------------------------------------------------------------
# prune_jobs — daily
# ---------------------------------------------------------------------------
# Retention: succeeded 7d, failed/cancelled 30d, dead 90d
# (architecture/02 §9, docs/phases/phase-03-background-jobs.md §Testing).

_JOB_RETENTION = {
    "succeeded": 7,
    "failed": 30,
    "cancelled": 30,
    "dead": 90,
}


async def handle_prune_jobs(payload: dict[str, Any]) -> None:
    """Prune terminal job rows by state-specific retention."""
    for state, days in _JOB_RETENTION.items():
        cutoff = datetime.now(UTC) - timedelta(days=days)
        async with get_session_factory()() as session:
            await _batch_delete(
                session,
                sql="""
                    DELETE FROM jobs
                    WHERE id IN (
                        SELECT id FROM jobs
                        WHERE state = :state
                          AND finished_at < :cutoff
                        LIMIT :batch
                    )
                """,
                params={"state": state, "cutoff": cutoff, "batch": _BATCH},
                label=f"prune_jobs[{state}]",
            )


# ---------------------------------------------------------------------------
# purge_run_events — daily
# ---------------------------------------------------------------------------

async def handle_purge_run_events(payload: dict[str, Any]) -> None:
    """Purge run_events rows > 24h after their run finished."""
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    async with get_session_factory()() as session:
        await _batch_delete(
            session,
            sql="""
                DELETE FROM run_events
                WHERE run_id IN (
                    SELECT r.id FROM runs r
                    WHERE r.finished_at < :cutoff
                    LIMIT :batch
                )
            """,
            params={"cutoff": cutoff, "batch": _BATCH},
            label="purge_run_events",
        )
