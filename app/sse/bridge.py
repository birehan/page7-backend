"""SSE-over-jobs bridge (architecture/12 §5).

Phase 3 created the `runs`/`run_events` tables; this module is their first
real writer/reader. A long-running operation inserts a `runs` row, enqueues a
job in the same transaction, and streams frames by polling `run_events` from
seq 1 — the client-side SSE reader ignores frame ids and cannot reconnect
mid-stream, so replay-from-start is mandatory.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.core.errors import ApiError
from app.core.time import utc_now
from app.db.session import get_session_factory
from app.jobs.models import Job
from app.sse.in_request import (
    KEEPALIVE_INTERVAL_SECONDS,
    format_sse_comment,
    format_sse_event,
    sse_response,
)
from app.sse.models import Run, RunEvent

_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "partial"})
_POLL_INTERVAL_SECONDS = 0.25
# Bridged runs (visuals_generate, brand_relearn, plan_commit) can take minutes;
# the in-request path keeps MAX_STREAM_SECONDS=120 for short LLM streams.
BRIDGE_MAX_STREAM_SECONDS = 600
# Fail fast only when the run's job stays unclaimed *and* its queue has no
# running work (dead worker). A queued job behind other running jobs is healthy
# backlog — do not stall those.
BRIDGE_QUEUED_STALL_SECONDS = 60


async def create_run(
    session: AsyncSession,
    *,
    organization_id: uuid.UUID,
    brand_id: uuid.UUID | None,
    kind: str,
    idempotency_key: str | None,
    request_hash: str,
    created_by: uuid.UUID | None,
) -> Run:
    """Insert a run, or return the existing one on an identical replay.

    A same-key / different-hash re-POST is `422 IDEMPOTENCY_MISMATCH`.
    """
    if idempotency_key is not None:
        existing = (
            await session.execute(
                sa.select(Run).where(
                    Run.organization_id == organization_id,
                    Run.kind == kind,
                    Run.idempotency_key == idempotency_key,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.request_hash != request_hash:
                raise ApiError(
                    "IDEMPOTENCY_MISMATCH",
                    "Idempotency-Key reused with a different request body",
                    status_code=422,
                )
            return existing

    run = Run(
        organization_id=organization_id,
        brand_id=brand_id,
        kind=kind,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        status="queued",
        created_by=created_by,
    )
    session.add(run)
    await session.flush()
    return run


async def mark_run_running(session: AsyncSession, run: Run) -> None:
    if run.status == "queued":
        run.status = "running"
        run.started_at = utc_now()
        await session.flush()


async def mark_run_finished(
    session: AsyncSession,
    run: Run,
    *,
    status: str,
    result: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> None:
    run.status = status
    run.result = result
    run.error = error
    run.finished_at = utc_now()
    await session.flush()


async def emit(
    session: AsyncSession,
    run_id: uuid.UUID,
    event: dict[str, Any],
) -> int:
    """Append one SSE frame. Returns the assigned seq.

    Callers that fan out concurrent work against the same run must serialize
    emit() themselves (an asyncio.Lock) — MAX(seq)+1 is not race-free under
    concurrent sessions.
    """
    next_seq = (
        await session.execute(
            sa.select(sa.func.coalesce(sa.func.max(RunEvent.seq), 0) + 1).where(
                RunEvent.run_id == run_id
            )
        )
    ).scalar_one()
    row = RunEvent(run_id=run_id, seq=int(next_seq), event=event)
    session.add(row)
    await session.flush()
    return int(next_seq)


async def _fetch_events_after(
    session: AsyncSession, run_id: uuid.UUID, after_seq: int
) -> list[RunEvent]:
    result = await session.execute(
        sa.select(RunEvent)
        .where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
        .order_by(RunEvent.seq.asc())
    )
    return list(result.scalars().all())


async def _get_run_status(session: AsyncSession, run_id: uuid.UUID) -> str | None:
    return (
        await session.execute(sa.select(Run.status).where(Run.id == run_id))
    ).scalar_one_or_none()


async def _backing_job_unclaimed_on_idle_queue(
    session: AsyncSession, *, run_id: uuid.UUID
) -> bool:
    """True when this run's job is still queued and its queue has no running work.

    Distinguishes a dead worker (nothing claimed) from a healthy backlog (other
    jobs running; this one waiting for a free concurrency slot). Claim sets
    `jobs.state='running'` before the handler can `mark_run_running`, so a
    claimed-but-not-yet-emitting job is not treated as stalled.
    """
    row = (
        await session.execute(
            sa.select(Job.state, Job.queue)
            .where(Job.payload["run_id"].astext == str(run_id))
            .order_by(Job.id.desc())
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return True
    state, queue = row
    if state != "queued":
        return False
    running = (
        await session.execute(
            sa.select(sa.func.count())
            .select_from(Job)
            .where(Job.queue == queue, Job.state == "running")
        )
    ).scalar_one()
    return int(running) == 0


async def stream_run_events(run_id: uuid.UUID) -> AsyncIterator[str]:
    """Poll `run_events` from seq 1, emitting SSE frames until the run terminates.

    Uses its own short-lived sessions so it does not hold a request transaction
    open for the full stream duration.
    """
    factory = get_session_factory()
    last_seq = 0
    started = time.monotonic()
    last_keepalive = started

    while True:
        now = time.monotonic()
        if now - started > BRIDGE_MAX_STREAM_SECONDS:
            yield format_sse_event(
                "message",
                json.dumps(
                    {"type": "error", "code": "STREAM_TIMEOUT", "message": "Stream timed out"}
                ),
            )
            return

        async with factory() as session:
            events = await _fetch_events_after(session, run_id, last_seq)
            status = await _get_run_status(session, run_id)
            job_idle_unclaimed = False
            if (
                status == "queued"
                and last_seq == 0
                and not events
                and now - started > BRIDGE_QUEUED_STALL_SECONDS
            ):
                job_idle_unclaimed = await _backing_job_unclaimed_on_idle_queue(
                    session, run_id=run_id
                )

        if (
            status == "queued"
            and last_seq == 0
            and not events
            and now - started > BRIDGE_QUEUED_STALL_SECONDS
            and job_idle_unclaimed
        ):
            # Job is left queued — caller may still claim it later; fail-fast UX
            # prefers a clear error over a 10-minute hang.
            yield format_sse_event(
                "message",
                json.dumps(
                    {
                        "type": "error",
                        "code": "JOB_NOT_STARTED",
                        "message": "Job did not leave the queue in time",
                    }
                ),
            )
            return

        if events:
            for row in events:
                # Frontend ignores the SSE `event:` name and reads `type` from JSON.
                yield format_sse_event("message", json.dumps(row.event, default=str))
                last_seq = row.seq
                last_keepalive = time.monotonic()
        elif now - last_keepalive >= KEEPALIVE_INTERVAL_SECONDS:
            yield format_sse_comment("keep-alive")
            last_keepalive = now

        if status in _TERMINAL and not events:
            # Drain once more in case the terminal emit raced with the status update.
            async with factory() as session:
                trailing = await _fetch_events_after(session, run_id, last_seq)
            for row in trailing:
                yield format_sse_event("message", json.dumps(row.event, default=str))
                last_seq = row.seq
            return

        await asyncio.sleep(_POLL_INTERVAL_SECONDS)


def bridge_sse_response(run_id: uuid.UUID) -> StreamingResponse:
    return sse_response(stream_run_events(run_id))
