"""Concurrent job claim: 10 claimers, 100 jobs, each claimed exactly once."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session_factory
from app.jobs import queue as job_queue


@pytest.mark.asyncio
async def test_ten_claimers_claim_one_hundred_jobs_exactly_once(
    db_session: AsyncSession,
) -> None:
    """architecture/03 test #4 / architecture/12 Testing / checklist §1 and §9."""
    await db_session.execute(text("DELETE FROM jobs"))
    await db_session.commit()

    for i in range(100):
        await job_queue.enqueue(
            db_session,
            queue="maintenance",
            type="maintenance.cleanup_sessions",
            payload={"n": i},
            unique_key=f"claim-test:{i}",
        )
    await db_session.commit()

    claimed: list[int] = []
    lock = asyncio.Lock()

    async def claimer(worker_id: str) -> None:
        async with get_session_factory()() as session:
            while True:
                rows = await job_queue.claim(
                    session,
                    queue="maintenance",
                    batch_size=5,
                    worker_id=worker_id,
                )
                if not rows:
                    return
                async with lock:
                    claimed.extend(int(r["id"]) for r in rows)

    await asyncio.gather(*(claimer(f"w-{i}-{uuid.uuid4().hex[:8]}") for i in range(10)))

    assert len(claimed) == 100
    assert len(set(claimed)) == 100
