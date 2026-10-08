"""A cron period is claimed and its job enqueued in ONE transaction.

If they were committed separately, a crash between the two would mark the period as done
while no job existed, and a daily or weekly job would silently never run.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.jobs import scheduler as scheduler_module
from app.jobs.scheduler import CronEntry, Scheduler


def _entry() -> CronEntry:
    return CronEntry(
        f"test.atomic.{uuid.uuid4().hex[:8]}",
        job_type="test.noop",
        queue="maintenance",
        period_seconds=3600,
    )


async def _claims(db: AsyncSession, name: str) -> int:
    row = await db.execute(text("SELECT count(*) FROM cron_runs WHERE name = :n"), {"n": name})
    return int(row.scalar_one())


async def _jobs(db: AsyncSession, name: str) -> int:
    row = await db.execute(
        text("SELECT count(*) FROM jobs WHERE unique_key LIKE :k"), {"k": f"cron:{name}:%"}
    )
    return int(row.scalar_one())


async def test_failed_enqueue_leaves_no_claim_so_the_period_is_retried(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = _entry()
    now = datetime.now(UTC)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("enqueue failed")

    monkeypatch.setattr(scheduler_module.q, "enqueue", boom)
    with pytest.raises(RuntimeError):
        await Scheduler(entries=[entry])._maybe_enqueue(entry, now)
    monkeypatch.undo()

    assert await _claims(db_session, entry.name) == 0
    assert await _jobs(db_session, entry.name) == 0

    # The next tick can still enqueue that same period.
    await Scheduler(entries=[entry])._maybe_enqueue(entry, now)
    assert await _claims(db_session, entry.name) == 1
    assert await _jobs(db_session, entry.name) == 1


async def test_a_period_is_enqueued_exactly_once(db_session: AsyncSession) -> None:
    entry = _entry()
    now = datetime.now(UTC)
    scheduler = Scheduler(entries=[entry])

    await scheduler._maybe_enqueue(entry, now)
    await scheduler._maybe_enqueue(entry, now)

    assert await _claims(db_session, entry.name) == 1
    assert await _jobs(db_session, entry.name) == 1
