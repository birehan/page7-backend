"""Job queue primitives: enqueue, claim, heartbeat, stale-lease sweep.

All SQL follows architecture/12 exactly.  The claim query uses
`FOR UPDATE SKIP LOCKED`; the heartbeat's return count is the fencing signal;
the stale-lease sweep requeues (with backoff) or marks dead.

Design notes (architecture/12 §Claim):
- Enqueue is a plain INSERT inside the CALLER'S OWN TRANSACTION — no outbox,
  no separate dispatcher.  A job is committed iff its owning business row is.
- The claiming transaction is short and ends at COMMIT, not across execution;
  the lease (lease_until) excludes other workers while the job is running.
- LISTEN/NOTIFY is deliberately not built (polling only, 1 s idle).
"""

from __future__ import annotations

import json
import logging
import random
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# --- per-type timeout defaults (seconds) -----------------------------------
# Stamped onto the row at enqueue time so the claim query's lease arithmetic
# has a concrete value without a join.  timeout_seconds + 30 = initial lease.
_JOB_TIMEOUT_SECONDS: dict[str, int] = {
    "email.send": 30,
    "maintenance.cleanup_sessions": 300,
    "maintenance.expire_idempotency_keys": 300,
    "maintenance.prune_rate_limits": 300,
    "maintenance.prune_jobs": 300,
    "maintenance.purge_run_events": 300,
    "media.process": 300,
    "media.cleanup_upload_intents": 300,
    # Callers: job worker lease + inline publish claim. User: post now forever /
    # error while posted. Must exceed Zernio publishNow timeout (90s) + headroom.
    "publish_post": 120,
    "reconcile_publications": 120,
    "sweep_publishing_invariants": 120,
    "ai.plan_commit": 300,
    "ai.brand_research": 300,
    "ai.visuals_generate": 600,
    "zernio_health_poll": 300,
    "process_webhook_event": 60,
    "analytics_sync": 300,
    "analytics_weekly_insights": 600,
    "inbox_sync": 300,
    "inbox_poll": 120,
    "inbox_classify_message": 120,
    "inbox_send_reply": 60,
    "reconcile_inbox_replies": 120,
    "billing.renew": 300,
    "billing.usage_alerts": 300,
}
_DEFAULT_TIMEOUT_SECONDS = 60

# --- backoff parameters per queue: (base_s, cap_s) -------------------------
# architecture/12 §Retry specifies publishing/webhooks/ai; the rest are
# documented defaults from docs/phases/phase-03-background-jobs.md §Risks.
_BACKOFF: dict[str, tuple[float, float]] = {
    "publishing": (30.0, 1800.0),
    "webhooks": (10.0, 600.0),
    "ai": (10.0, 300.0),
    "media": (10.0, 300.0),
    "sync": (10.0, 300.0),
    "maintenance": (60.0, 1800.0),
}
_DEFAULT_BACKOFF = (10.0, 300.0)


def timeout_for(job_type: str) -> int:
    return _JOB_TIMEOUT_SECONDS.get(job_type, _DEFAULT_TIMEOUT_SECONDS)


def backoff_run_at(queue: str, attempts: int) -> datetime:
    """Exponential backoff with jitter: min(cap, base * 2^attempts) * uniform(1.0, 1.5)."""
    base, cap = _BACKOFF.get(queue, _DEFAULT_BACKOFF)
    delay = min(cap, base * (2**attempts)) * random.uniform(1.0, 1.5)  # noqa: S311
    return datetime.now(UTC) + timedelta(seconds=delay)


# ---------------------------------------------------------------------------
# Enqueue — inside the caller's own transaction
# ---------------------------------------------------------------------------

async def enqueue(
    session: AsyncSession,
    *,
    queue: str,
    type: str,  # noqa: A002
    payload: dict[str, Any] | None = None,
    unique_key: str | None = None,
    run_at: datetime | None = None,
    priority: int = 100,
    max_attempts: int = 5,
    organization_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> int | None:
    """Enqueue a job inside the caller's current transaction.

    Returns the new job id when a row was inserted, or None when the
    unique_key ON CONFLICT guard deduplicated a live duplicate.

    The ON CONFLICT clause references the partial index
    ux_jobs_unique_key ON (unique_key) WHERE unique_key IS NOT NULL
    AND state IN ('queued','running').
    When unique_key is NULL that index never fires and the INSERT always succeeds.
    """
    full_payload: dict[str, Any] = dict(payload or {})
    if correlation_id:
        full_payload["_correlation_id"] = correlation_id

    result = await session.execute(
        sa.text(
            """
            INSERT INTO jobs
                (queue, type, payload, state, priority, run_at,
                 max_attempts, timeout_seconds, unique_key, organization_id)
            VALUES
                (:queue, :type, cast(:payload AS jsonb), 'queued', :priority, :run_at,
                 :max_attempts, :timeout_seconds, :unique_key, :organization_id)
            ON CONFLICT (unique_key)
                WHERE unique_key IS NOT NULL AND state IN ('queued', 'running')
                DO NOTHING
            RETURNING id
            """
        ),
        {
            "queue": queue,
            "type": type,
            "payload": json.dumps(full_payload),
            "priority": priority,
            "run_at": run_at or datetime.now(UTC),
            "max_attempts": max_attempts,
            "timeout_seconds": timeout_for(type),
            "unique_key": unique_key,
            "organization_id": str(organization_id) if organization_id else None,
        },
    )
    row = result.fetchone()
    return int(row[0]) if row else None


# ---------------------------------------------------------------------------
# Claim — called by the worker process, NOT the API
# ---------------------------------------------------------------------------

async def claim(
    session: AsyncSession,
    *,
    queue: str,
    batch_size: int,
    worker_id: str,
) -> list[dict[str, Any]]:
    """Claim up to batch_size queued jobs for this worker.

    Exact query from architecture/12:
        FOR UPDATE SKIP LOCKED on the queued set, then UPDATE in a CTE.
    Commits the session so the lock is released and the rows are visible to
    other connections (important for the concurrent-claim test).
    """
    result = await session.execute(
        sa.text(
            """
            WITH c AS (
                SELECT id FROM jobs
                WHERE queue = :queue
                  AND state = 'queued'
                  AND run_at <= now()
                ORDER BY priority, run_at, id
                LIMIT :batch
                FOR UPDATE SKIP LOCKED
            )
            UPDATE jobs j
            SET
                state       = 'running',
                locked_by   = :worker_id,
                locked_at   = now(),
                started_at  = now(),
                attempts    = j.attempts + 1,
                lease_until = now() + make_interval(secs => j.timeout_seconds + 30)
            FROM c
            WHERE j.id = c.id
            RETURNING j.*
            """
        ),
        {"queue": queue, "batch": batch_size, "worker_id": worker_id},
    )
    rows = [dict(r._mapping) for r in result.fetchall()]
    await session.commit()
    return rows


# ---------------------------------------------------------------------------
# Heartbeat — fencing signal
# ---------------------------------------------------------------------------

async def heartbeat(
    session: AsyncSession,
    *,
    ids: list[int],
    worker_id: str,
    timeout_seconds: int,
) -> set[int]:
    """Extend the lease on running jobs.

    Returns the set of ids whose lease was NOT updated — another process's
    stale-lease sweep reclaimed them; the runner must cancel those tasks.
    """
    if not ids:
        return set()
    result = await session.execute(
        sa.text(
            """
            UPDATE jobs
            SET lease_until = now() + make_interval(secs => :secs)
            WHERE id = ANY(:ids)
              AND locked_by = :worker_id
              AND state = 'running'
            RETURNING id
            """
        ),
        {"ids": ids, "worker_id": worker_id, "secs": timeout_seconds + 30},
    )
    await session.commit()
    updated = {row[0] for row in result.fetchall()}
    return set(ids) - updated  # reclaimed ids (fencing)


# ---------------------------------------------------------------------------
# Mark success / failure / dead
# ---------------------------------------------------------------------------

async def mark_succeeded(session: AsyncSession, *, job_id: int) -> None:
    await session.execute(
        sa.text(
            """
            UPDATE jobs
            SET state='succeeded', finished_at=now(),
                lease_until=NULL, locked_by=NULL
            WHERE id=:id
            """
        ),
        {"id": job_id},
    )
    await session.commit()


async def mark_failed(
    session: AsyncSession,
    *,
    job_id: int,
    queue: str,
    attempts: int,
    max_attempts: int,
    error: str,
) -> None:
    """Retry with backoff, or move to dead if max_attempts exhausted."""
    if attempts >= max_attempts:
        await session.execute(
            sa.text(
                """
                UPDATE jobs
                SET state='dead', finished_at=now(),
                    last_error=:error, lease_until=NULL, locked_by=NULL
                WHERE id=:id
                """
            ),
            {"id": job_id, "error": error[:2000]},
        )
    else:
        next_run = backoff_run_at(queue, attempts)
        await session.execute(
            sa.text(
                """
                UPDATE jobs
                SET state='queued', run_at=:run_at, last_error=:error,
                    locked_by=NULL, locked_at=NULL, started_at=NULL,
                    lease_until=NULL
                WHERE id=:id
                """
            ),
            {"id": job_id, "run_at": next_run, "error": error[:2000]},
        )
    await session.commit()


async def mark_dead(session: AsyncSession, *, job_id: int, error: str) -> None:
    """Unconditionally move a job to the `dead` state (terminal, no retry).

    Used by the runner for TerminalError and for unknown job types.
    """
    await session.execute(
        sa.text(
            """
            UPDATE jobs
            SET state='dead', finished_at=now(),
                last_error=:error, lease_until=NULL, locked_by=NULL
            WHERE id=:id
            """
        ),
        {"id": job_id, "error": error[:2000]},
    )
    await session.commit()


# ---------------------------------------------------------------------------
# SIGTERM graceful shutdown — release without incrementing attempts
# ---------------------------------------------------------------------------

async def release_to_queued(session: AsyncSession, *, ids: list[int]) -> None:
    """Return jobs to `queued` without incrementing `attempts`.

    A graceful shutdown is not a failed attempt — architecture/12 §Timeouts.
    """
    if not ids:
        return
    await session.execute(
        sa.text(
            """
            UPDATE jobs
            SET state='queued', run_at=now(),
                locked_by=NULL, locked_at=NULL, started_at=NULL,
                lease_until=NULL
            WHERE id = ANY(:ids) AND state = 'running'
            """
        ),
        {"ids": ids},
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Stale-lease sweep — run periodically inside each worker process
# ---------------------------------------------------------------------------

async def sweep_stale_leases(session: AsyncSession) -> int:
    """Requeue or dead-letter jobs whose lease expired without a heartbeat.

    A SIGKILL or crash is handled identically — by this sweep once the lease
    expires.  Returns the count of rows affected.
    """
    result = await session.execute(
        sa.text(
            """
            UPDATE jobs
            SET
                state = CASE
                    WHEN attempts < max_attempts THEN 'queued'
                    ELSE 'dead'
                END,
                run_at = CASE
                    WHEN attempts < max_attempts
                        THEN now() + interval '5 seconds'
                    ELSE run_at
                END,
                finished_at = CASE
                    WHEN attempts >= max_attempts THEN now()
                    ELSE finished_at
                END,
                last_error  = 'LEASE_EXPIRED',
                locked_by   = NULL,
                locked_at   = NULL,
                lease_until = NULL
            WHERE state = 'running'
              AND lease_until < now()
            RETURNING id
            """
        )
    )
    await session.commit()
    count = len(result.fetchall())
    if count:
        log.warning("stale_lease_sweep recovered %d job(s)", count)
    return count
