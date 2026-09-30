"""Cron scheduler — cron_runs-guarded, no leader election.

Architecture/12 §Scheduler:
    Every worker process runs the same scheduler loop.  Correctness comes from
    the database, not coordination: each entry computes its own current
    period_start and does INSERT INTO cron_runs ... ON CONFLICT DO NOTHING.
    A periodic job is enqueued ONLY when that INSERT actually inserted a row.

Phase 3 entries (the four maintenance sweeps + purge_run_events):
    cleanup_sessions       hourly
    expire_idempotency_keys  every 30 minutes
    prune_rate_limits        daily
    prune_jobs               daily
    purge_run_events         daily

Phase 9: zernio_health_poll hourly on the sync queue.
Phase 10: reconcile_publications + sweep_publishing_invariants every 60s
on the publishing queue.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.jobs import queue as q

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# CronEntry
# ---------------------------------------------------------------------------

class CronEntry:
    """One periodic job definition."""

    def __init__(
        self,
        name: str,
        *,
        job_type: str,
        queue: str,
        period_seconds: int,
        priority: int = 200,
        max_attempts: int = 3,
        anchor: datetime | None = None,
    ) -> None:
        self.name = name
        self.job_type = job_type
        self.queue = queue
        self.period_seconds = period_seconds
        self.priority = priority
        self.max_attempts = max_attempts
        self.anchor = anchor

    def current_period_start(self, now: datetime) -> datetime:
        """Floor `now` to the current period boundary."""
        epoch = self.anchor if self.anchor is not None else datetime(2020, 1, 1, tzinfo=UTC)
        elapsed = (now - epoch).total_seconds()
        floored = int(elapsed // self.period_seconds) * self.period_seconds
        return epoch + timedelta(seconds=floored)


_HOUR = 3600
_HALF_HOUR = 1800
_DAY = 86400
_MINUTE = 60
_WEEK = 604800
_FIFTEEN_MIN = 900
_FIVE_MIN = 300

ENTRIES: list[CronEntry] = [
    CronEntry(
        "maintenance.cleanup_sessions",
        job_type="maintenance.cleanup_sessions",
        queue="maintenance",
        period_seconds=_HOUR,
    ),
    CronEntry(
        "maintenance.expire_idempotency_keys",
        job_type="maintenance.expire_idempotency_keys",
        queue="maintenance",
        period_seconds=_HALF_HOUR,
    ),
    CronEntry(
        "maintenance.prune_rate_limits",
        job_type="maintenance.prune_rate_limits",
        queue="maintenance",
        period_seconds=_DAY,
    ),
    CronEntry(
        "maintenance.prune_jobs",
        job_type="maintenance.prune_jobs",
        queue="maintenance",
        period_seconds=_DAY,
    ),
    CronEntry(
        "maintenance.purge_run_events",
        job_type="maintenance.purge_run_events",
        queue="maintenance",
        period_seconds=_DAY,
    ),
    CronEntry(
        "cultural_calendar.extend_catalog",
        job_type="cultural_calendar.extend_catalog",
        queue="maintenance",
        period_seconds=_DAY,
    ),
    CronEntry(
        "media.cleanup_upload_intents",
        job_type="media.cleanup_upload_intents",
        queue="maintenance",
        period_seconds=_DAY,
    ),
    CronEntry(
        "zernio_health_poll",
        job_type="zernio_health_poll",
        queue="sync",
        period_seconds=_HOUR,
    ),
    CronEntry(
        "reconcile_publications",
        job_type="reconcile_publications",
        queue="publishing",
        period_seconds=_MINUTE,
        priority=50,
    ),
    CronEntry(
        "sweep_publishing_invariants",
        job_type="sweep_publishing_invariants",
        queue="publishing",
        period_seconds=_MINUTE,
        priority=50,
    ),
    CronEntry(
        "analytics_sync",
        job_type="analytics_sync",
        queue="sync",
        period_seconds=_FIFTEEN_MIN,
    ),
    CronEntry(
        "analytics_weekly_insights",
        job_type="analytics_weekly_insights",
        queue="ai",
        period_seconds=_WEEK,
        # Monday 06:00 Asia/Riyadh == Monday 03:00 UTC (no DST).
        anchor=datetime(2020, 1, 6, 3, 0, tzinfo=UTC),
    ),
    CronEntry(
        "reconcile_inbox_replies",
        job_type="reconcile_inbox_replies",
        queue="sync",
        period_seconds=_MINUTE,
        priority=50,
    ),
    CronEntry(
        "inbox_poll",
        job_type="inbox_poll",
        queue="sync",
        period_seconds=_FIVE_MIN,
        priority=80,
    ),
    CronEntry(
        "billing.renew",
        job_type="billing.renew",
        queue="maintenance",
        period_seconds=_DAY,
    ),
    CronEntry(
        "billing.usage_alerts",
        job_type="billing.usage_alerts",
        queue="maintenance",
        period_seconds=_DAY,
    ),
]


# ---------------------------------------------------------------------------
# Scheduler main loop
# ---------------------------------------------------------------------------

class Scheduler:
    """Runs the cron-scheduler loop.  Instantiate one per process."""

    def __init__(self, *, entries: list[CronEntry] | None = None) -> None:
        self._entries = entries or ENTRIES
        self._settings = get_settings().jobs
        self._shutdown = asyncio.Event()

    async def run(self) -> None:
        import signal

        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGTERM, self._shutdown.set)
        loop.add_signal_handler(signal.SIGINT, self._shutdown.set)

        log.info("scheduler.started", entries=[e.name for e in self._entries])
        while not self._shutdown.is_set():
            await self._tick()
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=self._settings.scheduler_interval_seconds,
                )
            except TimeoutError:
                pass
        log.info("scheduler.stopped")

    async def _tick(self) -> None:
        now = datetime.now(UTC)
        for entry in self._entries:
            try:
                await self._maybe_enqueue(entry, now)
            except Exception:
                log.exception("scheduler.entry_error", entry=entry.name)

    async def _maybe_enqueue(self, entry: CronEntry, now: datetime) -> None:
        period_start = entry.current_period_start(now)
        async with get_session_factory()() as session:
            inserted = await _insert_cron_run(session, name=entry.name, period_start=period_start)
            if not inserted:
                return  # already enqueued by another scheduler instance this period
            job_id = await q.enqueue(
                session,
                queue=entry.queue,
                type=entry.job_type,
                payload={},
                unique_key=f"cron:{entry.name}:{int(period_start.timestamp())}",
                priority=entry.priority,
                max_attempts=entry.max_attempts,
            )
            await session.commit()
        if job_id:
            log.info(
                "scheduler.enqueued",
                entry=entry.name,
                period_start=period_start.isoformat(),
                job_id=job_id,
            )


async def _insert_cron_run(
    session: AsyncSession, *, name: str, period_start: datetime
) -> bool:
    """INSERT INTO cron_runs ... ON CONFLICT DO NOTHING RETURNING name.

    Returns True iff the INSERT actually inserted (not a no-op from conflict).
    """
    from sqlalchemy import text

    result = await session.execute(
        text(
            """
            INSERT INTO cron_runs (name, period_start)
            VALUES (:name, :period_start)
            ON CONFLICT DO NOTHING
            RETURNING name
            """
        ),
        {"name": name, "period_start": period_start},
    )
    await session.commit()
    return result.fetchone() is not None
