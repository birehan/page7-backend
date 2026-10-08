from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def test_liveness_never_touches_the_database(client: AsyncClient) -> None:
    response = await client.get("/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readiness_reaches_the_database(client: AsyncClient) -> None:
    response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.fixture
async def clean_queue(db_session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """The deep check looks at global queue state, which other tests leave behind.

    Clean before AND after: a stale scheduler row left by one of these tests would make
    `/health/deep` report "degraded" for every test that runs later, in this run or the next.
    """

    async def wipe() -> None:
        await db_session.execute(text("DELETE FROM jobs WHERE state = 'queued'"))
        await db_session.execute(
            text("DELETE FROM cron_runs WHERE name = 'reconcile_publications'")
        )
        await db_session.commit()

    await wipe()
    yield db_session
    await wipe()


async def test_deep_health_is_ok_when_idle(client: AsyncClient, clean_queue: AsyncSession) -> None:
    response = await client.get("/health/deep")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["database"]["ok"] is True
    assert body["checks"]["jobQueue"] == {"ok": True, "overdueJobs": 0, "oldestOverdueSeconds": 0}
    # Outside production a scheduler that has never run is not a failure.
    assert body["checks"]["scheduler"] == {"ok": True, "lastRunSecondsAgo": None}


async def test_deep_health_fails_when_a_job_is_long_overdue(
    client: AsyncClient, clean_queue: AsyncSession
) -> None:
    await clean_queue.execute(
        text(
            "INSERT INTO jobs (queue, type, state, run_at) "
            "VALUES ('maintenance', 'test.overdue', 'queued', now() - interval '30 minutes')"
        )
    )
    await clean_queue.commit()

    response = await client.get("/health/deep")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["jobQueue"]["ok"] is False
    assert body["checks"]["jobQueue"]["overdueJobs"] == 1
    assert body["checks"]["jobQueue"]["oldestOverdueSeconds"] >= 1700
    await clean_queue.execute(text("DELETE FROM jobs WHERE type = 'test.overdue'"))
    await clean_queue.commit()


async def test_deep_health_tolerates_a_briefly_delayed_job(
    client: AsyncClient, clean_queue: AsyncSession
) -> None:
    await clean_queue.execute(
        text(
            "INSERT INTO jobs (queue, type, state, run_at) "
            "VALUES ('maintenance', 'test.delayed', 'queued', now() - interval '3 minutes')"
        )
    )
    await clean_queue.commit()

    response = await client.get("/health/deep")

    assert response.status_code == 200
    assert response.json()["checks"]["jobQueue"]["overdueJobs"] == 1
    await clean_queue.execute(text("DELETE FROM jobs WHERE type = 'test.delayed'"))
    await clean_queue.commit()


async def test_deep_health_fails_when_the_scheduler_stopped(
    client: AsyncClient, clean_queue: AsyncSession
) -> None:
    await clean_queue.execute(
        text(
            "INSERT INTO cron_runs (name, period_start) "
            "VALUES ('reconcile_publications', now() - interval '1 hour')"
        )
    )
    await clean_queue.commit()

    response = await client.get("/health/deep")

    assert response.status_code == 503
    scheduler = response.json()["checks"]["scheduler"]
    assert scheduler["ok"] is False
    assert scheduler["lastRunSecondsAgo"] >= 3500


async def test_deep_health_is_ok_with_a_recent_scheduler_run(
    client: AsyncClient, clean_queue: AsyncSession
) -> None:
    await clean_queue.execute(
        text(
            "INSERT INTO cron_runs (name, period_start) "
            "VALUES ('reconcile_publications', now() - interval '30 seconds')"
        )
    )
    await clean_queue.commit()

    response = await client.get("/health/deep")

    assert response.status_code == 200
    assert response.json()["checks"]["scheduler"]["ok"] is True
