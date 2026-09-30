"""Per-queue claim loops, timeout enforcement, and graceful shutdown.

Architecture/12 §Concurrency and §Timeouts govern this module.

Key invariants:
- One worker process holds one claim loop per queue, bounded by a Semaphore.
- Each claimed job runs under asyncio.wait_for(handler, timeout_seconds).
- Heartbeat fires every 1/3 of timeout_seconds; if an id is not returned
  by the heartbeat UPDATE it was reclaimed by the stale-lease sweep → the
  local task is cancelled (fencing).
- SIGTERM: stop claiming, wait ≤ grace_period_seconds, release survivors to
  'queued' WITHOUT incrementing their attempt count.
"""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Awaitable, Callable
from typing import Any

import structlog

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.jobs import queue as q
from app.jobs.errors import RetryableError, TerminalError

log = structlog.get_logger(__name__)

# --- per-queue concurrency limits (architecture/12 §Concurrency) -----------
_CONCURRENCY: dict[str, int] = {
    "publishing": 8,
    "webhooks": 8,
    "ai": 4,
    "media": 4,
    "sync": 2,
    "maintenance": 1,
}
_DEFAULT_CONCURRENCY = 2

Handler = Callable[[dict[str, Any]], Awaitable[None]]


class Worker:
    """Runs independent claim loops for each configured queue.

    Usage::

        worker = Worker(registry={"email.send": handle_email_send, ...})
        await worker.run()   # blocks until SIGTERM
    """

    def __init__(
        self,
        registry: dict[str, Handler],
        *,
        queues: dict[str, int] | None = None,
        worker_id: str | None = None,
    ) -> None:
        self._registry = registry
        self._queue_concurrency = queues or _CONCURRENCY
        self._worker_id = worker_id or f"worker-{os.getpid()}"
        self._settings = get_settings().jobs
        self._shutdown = asyncio.Event()
        # job_id → asyncio.Task (for in-flight tracking)
        self._running: dict[int, asyncio.Task[None]] = {}

    async def run(self) -> None:
        """Start one claim loop per queue; handle SIGTERM gracefully."""
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self._handle_sigterm)
        loop.add_signal_handler(signal.SIGINT, self._handle_sigterm)

        tasks: list[asyncio.Task[None]] = []
        for queue_name, concurrency in self._queue_concurrency.items():
            tasks.append(
                asyncio.create_task(
                    self._claim_loop(queue_name, concurrency),
                    name=f"claim-{queue_name}",
                )
            )
        tasks.append(
            asyncio.create_task(self._sweep_loop(), name="stale-lease-sweep")
        )

        log.info("worker.started", worker_id=self._worker_id, queues=list(self._queue_concurrency))
        await asyncio.gather(*tasks, return_exceptions=True)
        log.info("worker.stopped", worker_id=self._worker_id)

    def _handle_sigterm(self) -> None:
        log.info("worker.sigterm_received", worker_id=self._worker_id)
        self._shutdown.set()

    async def _claim_loop(self, queue_name: str, concurrency: int) -> None:
        sem = asyncio.Semaphore(concurrency)
        poll = self._settings.poll_interval_seconds

        while not self._shutdown.is_set():
            try:
                async with get_session_factory()() as session:
                    jobs = await q.claim(
                        session,
                        queue=queue_name,
                        batch_size=concurrency,
                        worker_id=self._worker_id,
                    )

                if not jobs:
                    try:
                        await asyncio.wait_for(
                            self._shutdown.wait(), timeout=poll
                        )
                    except TimeoutError:
                        pass
                    continue

                for job in jobs:
                    task = asyncio.create_task(
                        self._run_job(job, sem),
                        name=f"job-{job['id']}",
                    )
                    self._running[int(job["id"])] = task
                    _jid = int(job["id"])

                    def _make_cb(_jid: int) -> Callable[[asyncio.Task[None]], None]:
                        def _cb(t: asyncio.Task[None]) -> None:
                            self._running.pop(_jid, None)

                        return _cb

                    task.add_done_callback(_make_cb(_jid))

            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("claim_loop.error", queue=queue_name)
                await asyncio.sleep(poll)

        # SIGTERM: wait grace period for in-flight tasks
        await self._graceful_shutdown()

    async def _run_job(self, job: dict[str, Any], sem: asyncio.Semaphore) -> None:
        job_id = int(job["id"])
        job_type = job["type"]
        timeout = int(job.get("timeout_seconds", 60))

        structlog.contextvars.bind_contextvars(
            job_id=job_id,
            job_type=job_type,
            attempt=job.get("attempts", 1),
            organization_id=str(job.get("organization_id") or ""),
            correlation_id=(job.get("payload") or {}).get("_correlation_id", ""),
        )

        handler = self._registry.get(job_type)
        if handler is None:
            log.error("job.no_handler", job_type=job_type)
            async with get_session_factory()() as session:
                await q.mark_dead(session, job_id=job_id, error=f"NO_HANDLER:{job_type}")
            structlog.contextvars.clear_contextvars()
            return

        async with sem:
            # Start heartbeat task at 1/3 of timeout
            heartbeat_task = asyncio.create_task(
                self._heartbeat_loop(job_id, timeout),
                name=f"heartbeat-{job_id}",
            )

            try:
                log.info("job.started")
                payload = job.get("payload") or {}
                await asyncio.wait_for(handler(payload), timeout=float(timeout))
                heartbeat_task.cancel()

                async with get_session_factory()() as session:
                    await q.mark_succeeded(session, job_id=job_id)
                log.info("job.succeeded")

            except TimeoutError:
                heartbeat_task.cancel()
                log.warning("job.timed_out", timeout=timeout)
                async with get_session_factory()() as session:
                    await q.mark_failed(
                        session,
                        job_id=job_id,
                        queue=job["queue"],
                        attempts=int(job.get("attempts", 1)),
                        max_attempts=int(job.get("max_attempts", 5)),
                        error="TIMEOUT",
                    )

            except asyncio.CancelledError:
                # Task was cancelled by fencing (heartbeat found the lease gone)
                # or by SIGTERM shutdown — do not touch the job row; the
                # stale-lease sweep or release_to_queued handles it.
                heartbeat_task.cancel()
                log.info("job.cancelled_by_fencing_or_shutdown")
                raise

            except RetryableError as exc:
                heartbeat_task.cancel()
                log.warning("job.retryable_error", after=exc.after)
                run_at = None
                if exc.after is not None:
                    from datetime import UTC, datetime, timedelta

                    run_at = datetime.now(UTC) + timedelta(seconds=exc.after)
                async with get_session_factory()() as session:
                    if run_at:
                        from sqlalchemy import text

                        await session.execute(
                            text(
                                """
                                UPDATE jobs SET state='queued', run_at=:run_at,
                                    last_error=:err, locked_by=NULL, locked_at=NULL,
                                    started_at=NULL, lease_until=NULL
                                WHERE id=:id
                                """
                            ),
                            {"id": job_id, "run_at": run_at, "err": str(exc)},
                        )
                        await session.commit()
                    else:
                        await q.mark_failed(
                            session,
                            job_id=job_id,
                            queue=job["queue"],
                            attempts=int(job.get("attempts", 1)),
                            max_attempts=int(job.get("max_attempts", 5)),
                            error=str(exc),
                        )

            except TerminalError as exc:
                heartbeat_task.cancel()
                log.error("job.terminal_error", code=exc.code)
                async with get_session_factory()() as session:
                    await q.mark_dead(session, job_id=job_id, error=exc.code)

            except Exception as exc:
                heartbeat_task.cancel()
                log.exception("job.unhandled_error")
                async with get_session_factory()() as session:
                    await q.mark_failed(
                        session,
                        job_id=job_id,
                        queue=job["queue"],
                        attempts=int(job.get("attempts", 1)),
                        max_attempts=int(job.get("max_attempts", 5)),
                        error=repr(exc)[:2000],
                    )

            finally:
                structlog.contextvars.clear_contextvars()

    async def _heartbeat_loop(self, job_id: int, timeout_seconds: int) -> None:
        """Fire heartbeat every 1/3 of timeout; cancel self if lease is gone."""
        interval = max(1.0, timeout_seconds / 3.0)
        while True:
            await asyncio.sleep(interval)
            try:
                async with get_session_factory()() as session:
                    reclaimed = await q.heartbeat(
                        session,
                        ids=[job_id],
                        worker_id=self._worker_id,
                        timeout_seconds=timeout_seconds,
                    )
                if job_id in reclaimed:
                    log.warning("job.lease_reclaimed_cancelling", job_id=job_id)
                    # Cancel the parent job task (fencing)
                    for task_id, task in list(self._running.items()):
                        if task_id == job_id:
                            task.cancel()
                    return
            except Exception:
                log.exception("heartbeat.error", job_id=job_id)

    async def _sweep_loop(self) -> None:
        """Run the stale-lease sweep every stale_lease_check_seconds."""
        interval = self._settings.stale_lease_check_seconds
        while not self._shutdown.is_set():
            try:
                await asyncio.wait_for(self._shutdown.wait(), timeout=interval)
            except TimeoutError:
                pass
            try:
                async with get_session_factory()() as session:
                    await q.sweep_stale_leases(session)
            except Exception:
                log.exception("sweep_stale_leases.error")

    async def _graceful_shutdown(self) -> None:
        """Wait up to grace_period_seconds then release survivors to queued."""
        grace = self._settings.grace_period_seconds
        if not self._running:
            return

        log.info(
            "worker.graceful_shutdown",
            in_flight=len(self._running),
            grace_seconds=grace,
        )
        _, pending = await asyncio.wait(
            list(self._running.values()),
            timeout=grace,
        )
        if pending:
            ids = list(self._running.keys())
            log.warning("worker.releasing_stalled_jobs", count=len(pending))
            for task in pending:
                task.cancel()
            async with get_session_factory()() as session:
                await q.release_to_queued(session, ids=ids)
